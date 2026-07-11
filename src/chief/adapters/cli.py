"""The CLI platform stack: bind the client-plane socket to a real engine turn (#131).

A third engine stack alongside Telegram and Discord, but its "platform" is the always-on
unix socket (:mod:`chief.client_plane`), not a chat network. :class:`CliTaskIO` turns
engine output into outbound frames (milestone / reply / file), broadcast to every client
tagged by ``thread_key`` (clients filter — forward-compatible with #134 subscriptions).
:class:`CliAdapter` owns the inbound side: it installs itself as the socket's frame
handler and routes ``user`` frames into ``engine.dispatch`` and ``command`` frames
through the shared owner :class:`~chief.adapters.commands.CommandRegistry`.

Owner tier only (#128 local trust): the 0600 socket mode *is* the auth, so a CLI turn
runs the full owner path (:meth:`Engine.dispatch`), never the guest receptionist.
"""

import asyncio
import itertools
import logging

from ..client_plane import (
    TYPE_COMMAND,
    TYPE_USER,
    FrameSender,
    SocketServer,
    error_frame,
    file_frame,
    milestone_frame,
    reply_frame,
)
from ..gate.approvals import ApprovalCard
from .base import Adapter, BudgetCard, Engine, MemoryReader, ReadyHook, Surface
from .commands import OWNER_COMMANDS, CommandContext, CommandRegistry

logger = logging.getLogger(__name__)

#: The split-vs-file cap for the CLI (M8). An *int*, never ``inf`` — the engine slices
#: with it (``split_message``). The socket carries whole frames, so nothing splits in
#: practice; ``should_send_as_file`` still fires at ``CLI_LIMIT ×
#: FILE_THRESHOLD_FACTOR`` (~4 MB) or on an un-splittable oversized code fence, which
#: becomes one file frame.
CLI_LIMIT = 1_000_000

#: The milestone marker the engine prefixes onto a progress line
#: (``core.tasks._run_turn``, test-pinned at tasks.py:1729). Detecting it here — rather
#: than adding a ``send_milestone`` Protocol method — keeps the blast radius to this one
#: IO (that method would touch TaskIO + _run_turn + every other IO + every fake).
_MILESTONE_PREFIX = "· "


class CliTaskIO:
    """Engine → client-plane output: broadcast milestone / reply / file frames (#131).

    Structural, like :class:`~chief.adapters.telegram.TelegramTaskIO`: it satisfies the
    engine's ``TaskIO`` plus the approval ``ApprovalIO`` and budget ``BudgetIO`` at
    once, so cards flow out the same socket as replies. Every frame is broadcast to all
    clients tagged with its ``thread_key``; a client shows only the threads it wants.
    """

    def __init__(self, server: SocketServer) -> None:
        self._server = server
        #: Monotonic source for spawned-topic keys (``cli:t1``, ``cli:t2``, …). Unique
        #: per process; the socket has no forum, so a synthetic key is all a topic uses.
        self._thread_ids = itertools.count(1)

    async def send(self, thread_key: str, text: str) -> None:
        """Broadcast a progress line as a milestone frame, else a reply frame.

        The engine streams milestones through the same ``send`` seam as final replies,
        distinguished only by the ``· `` prefix; we split them back into the two frame
        types so a client can render progress and answers differently.
        """
        if text.startswith(_MILESTONE_PREFIX):
            body = text.removeprefix(_MILESTONE_PREFIX)
            await self._server.broadcast(milestone_frame(thread_key, body))
        else:
            await self._server.broadcast(reply_frame(thread_key, text))

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None:
        """Broadcast an oversized reply as a base64 file frame (M8 long-reply path)."""
        await self._server.broadcast(file_frame(thread_key, filename, data, caption))

    async def create_thread(self, *, like_thread_key: str, title: str) -> str:
        """Mint a synthetic thread key for a spawned topic (the socket has no forum)."""
        return f"cli:t{next(self._thread_ids)}"

    async def archive_thread(self, thread_key: str) -> None:
        """No-op: a synthetic CLI thread has no forum topic to close."""
        logger.debug("cli archive_thread no-op", extra={"thread_key": thread_key})

    async def send_card(self, route: str, card: ApprovalCard) -> str:
        """Post an approval card as a plain reply frame; return its edit ref (#131).

        The approval *buttons* have no wire form until the approval-frame slice, so the
        card is visible but unanswerable — an unanswered card times out to deny, the
        safe default. The ref encodes the route so :meth:`edit_card` can find it.
        """
        await self._server.broadcast(reply_frame(route, card.text))
        return f"{route}|{card.approval_id}"

    async def edit_card(self, msg_ref: str, text: str) -> None:
        """Broadcast a card's outcome text to its route (no in-place edit on socket).

        ``rsplit`` (not ``split``): the route is a client-supplied ``thread_key`` that
        may itself contain ``|``, but the appended ``approval_id`` is numeric, so
        splitting from the right recovers the exact route.
        """
        route = msg_ref.rsplit("|", 1)[0]
        await self._server.broadcast(reply_frame(route, text))

    async def send_budget_card(self, route: str, card: BudgetCard) -> None:
        """Post the budget choice card as a reply frame (visible, unanswerable)."""
        await self._server.broadcast(reply_frame(route, card.text))


class CliAdapter(Adapter):
    """Inbound side of the CLI stack: route socket frames into the engine (#131).

    Installs itself as the socket's frame handler in ``__init__`` (before the server is
    run), so construction wires the plane without a live connection. ``run`` parks until
    ``stop`` — there is no connection to poll; the socket server owns that loop.
    """

    def __init__(
        self,
        *,
        server: SocketServer,
        engine: Engine,
        memory: MemoryReader | None = None,
        commands: CommandRegistry = OWNER_COMMANDS,
    ) -> None:
        self._server = server
        self._engine = engine
        self._memory = memory
        self._commands = commands
        self._stopped = asyncio.Event()
        server.set_handler(self._on_frame)

    async def run(self, on_ready: ReadyHook | None = None) -> None:
        """Signal readiness, then park until :meth:`stop` (the socket drives I/O)."""
        if on_ready is not None:
            await on_ready()
        await self._stopped.wait()

    async def stop(self) -> None:
        """Release :meth:`run` and detach the handler so no frame dispatches after.

        Clearing the handler closes the shutdown window: without it, a frame arriving
        after ``manager.shutdown()`` but before ``socket_server.stop()`` would dispatch
        into an already-torn-down engine and spawn a zombie session. After this, an
        inbound frame falls through to the server's ``unknown_type`` (harmless).
        """
        self._server.set_handler(None)
        self._stopped.set()

    async def _on_frame(self, frame: object, sender: FrameSender) -> bool:
        """Dispatch one inbound frame; return ``True`` iff this adapter owns its type.

        ``user`` → an owner engine turn; ``command`` → the shared owner registry, with
        the unknown-command error produced *here* (the registry keeps its silent-no-op
        contract). Anything else → ``False``, so the server answers ``unknown_type`` and
        every #130 test stays green. Dispatch runs inline on the connection's read loop
        (matching the chat adapters' sequential profile): it can await DB work and, on a
        busy thread, a stop-intent classifier round-trip, so a frame is head-of-line
        blocked behind the prior frame's dispatch — acceptable for a single owner.
        """
        ftype = _get(frame, "type")
        if ftype == TYPE_USER:
            return await self._handle_user(frame, sender)
        if ftype == TYPE_COMMAND:
            return await self._handle_command(frame, sender)
        return False

    async def _handle_user(self, frame: object, sender: FrameSender) -> bool:
        """Route a ``user`` frame into a real owner engine turn (#131)."""
        thread_key, text = _get(frame, "thread_key"), _get(frame, "text")
        if not _nonempty_str(thread_key) or not _nonempty_str(text):
            await sender(
                error_frame("invalid_fields", "user frame needs thread_key and text")
            )
            return True
        assert isinstance(thread_key, str) and isinstance(text, str)
        await self._engine.dispatch(
            thread_key=thread_key, text=text, surface=Surface.DM
        )
        return True

    async def _handle_command(self, frame: object, sender: FrameSender) -> bool:
        """Route a ``command`` frame through the shared owner registry (#131)."""
        thread_key, name = _get(frame, "thread_key"), _get(frame, "name")
        arg = _get(frame, "arg", "")
        if (
            not _nonempty_str(thread_key)
            or not _nonempty_str(name)
            or not isinstance(arg, str)
        ):
            await sender(
                error_frame(
                    "invalid_fields",
                    "command frame needs thread_key, name, and a string arg",
                )
            )
            return True
        assert isinstance(thread_key, str) and isinstance(name, str)
        if name not in self._commands.names():
            await sender(error_frame("unknown_command", f"unknown command: {name!r}"))
            return True

        async def reply(reply_text: str) -> None:
            await sender(reply_frame(thread_key, reply_text))

        ctx = CommandContext(
            engine=self._engine,
            memory=self._memory,
            thread_key=thread_key,
            arg=arg.strip(),
            is_casual=False,
            reply=reply,
        )
        await self._commands.dispatch(name, ctx)
        return True


def _get(frame: object, field: str, default: object = None) -> object:
    """Read a field off a decoded frame, or ``default`` if absent / not a mapping."""
    if isinstance(frame, dict):
        return frame.get(field, default)
    return default


def _nonempty_str(value: object) -> bool:
    """True for a non-empty ``str`` — the shape every required client field must be."""
    return isinstance(value, str) and bool(value)
