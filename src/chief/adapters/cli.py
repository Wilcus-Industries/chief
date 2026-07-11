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

Both sides share a :class:`~chief.persistence.messages.MessageLog` (#132): every inbound
owner message and outbound chief frame is recorded, and an outbound frame emitted while
no client is attached is logged *held*. On the next connection the adapter's connect
hook claim-replays those held frames (each marked ``"replay": true``) before live
traffic resumes — detach-replay, restart-proof because the log is the mechanism.
"""

import asyncio
import itertools
import json
import logging

from ..client_plane import (
    CLI_PLATFORM,
    TYPE_COMMAND,
    TYPE_REPLY,
    TYPE_USER,
    FrameSender,
    SocketServer,
    error_frame,
    file_frame,
    milestone_frame,
    reply_frame,
)
from ..gate.approvals import ApprovalCard
from ..persistence.messages import REPLAY_LIMIT, ROLE_CHIEF, ROLE_OWNER, MessageLog
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

    def __init__(self, server: SocketServer, *, log: MessageLog | None = None) -> None:
        self._server = server
        #: The shared message log (#132). ``None`` ⇒ broadcast-only, exactly #131's
        #: behavior (no held-frame recording, so no detach-replay).
        self._log = log
        #: Monotonic source for spawned-topic keys (``cli:t1``, ``cli:t2``, …). Unique
        #: per process; the socket has no forum, so a synthetic key is all a topic uses.
        self._thread_ids = itertools.count(1)

    async def _emit(
        self, thread_key: str, frame: dict[str, object], *, text: str
    ) -> None:
        """Broadcast one outbound frame and log it as delivered-or-held (#132).

        ``delivered`` is snapshotted BEFORE the broadcast: broadcast-before-record means
        a frame racing a new attach can never be double-delivered — worst case it is
        recorded held (the joining client is not yet in the broadcast set) and defers to
        that client's next attach. The stored ``payload`` is the whole wire frame, the
        replay source, so a missed frame re-delivers intact.
        """
        delivered = self._server.has_clients  # snapshot before broadcast
        await self._server.broadcast(frame)
        if self._log is not None:
            await self._log.record(
                platform=CLI_PLATFORM,
                thread_key=thread_key,
                role=ROLE_CHIEF,
                surface=Surface.DM.value,
                kind=str(frame["type"]),
                text=text,
                payload=json.dumps(frame),
                delivered=delivered,
            )

    async def send(self, thread_key: str, text: str) -> None:
        """Broadcast a progress line as a milestone frame, else a reply frame.

        The engine streams milestones through the same ``send`` seam as final replies,
        distinguished only by the ``· `` prefix; we split them back into the two frame
        types so a client can render progress and answers differently.
        """
        if text.startswith(_MILESTONE_PREFIX):
            body = text.removeprefix(_MILESTONE_PREFIX)
            await self._emit(thread_key, milestone_frame(thread_key, body), text=body)
        else:
            await self._emit(thread_key, reply_frame(thread_key, text), text=text)

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None:
        """Broadcast an oversized reply as a base64 file frame (M8 long-reply path).

        The whole base64 payload is stored in the log, so a file missed while detached
        replays intact (at the cost of fattening the sqlite file — accepted for a
        single-owner local plane).
        """
        await self._emit(
            thread_key,
            file_frame(thread_key, filename, data, caption),
            text=caption or filename,
        )

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
        await self._emit(route, reply_frame(route, card.text), text=card.text)
        return f"{route}|{card.approval_id}"

    async def edit_card(self, msg_ref: str, text: str) -> None:
        """Broadcast a card's outcome text to its route (no in-place edit on socket).

        ``rsplit`` (not ``split``): the route is a client-supplied ``thread_key`` that
        may itself contain ``|``, but the appended ``approval_id`` is numeric, so
        splitting from the right recovers the exact route.
        """
        route = msg_ref.rsplit("|", 1)[0]
        await self._emit(route, reply_frame(route, text), text=text)

    async def send_budget_card(self, route: str, card: BudgetCard) -> None:
        """Post the budget choice card as a reply frame (visible, unanswerable)."""
        await self._emit(route, reply_frame(route, card.text), text=card.text)


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
        log: MessageLog | None = None,
        replay_limit: int = REPLAY_LIMIT,
    ) -> None:
        self._server = server
        self._engine = engine
        self._memory = memory
        self._commands = commands
        #: The shared message log (#132). ``None`` ⇒ no inbound recording and no
        #: detach-replay: the adapter is exactly its #131 self.
        self._log = log
        self._replay_limit = replay_limit
        self._stopped = asyncio.Event()
        server.set_handler(self._on_frame)
        server.set_connect_hook(self._on_connect)

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
        inbound frame falls through to the server's ``unknown_type`` (harmless). The
        connect hook is cleared for the same reason: a connection accepted in that same
        window must not claim-replay against a torn-down log.
        """
        self._server.set_handler(None)
        self._server.set_connect_hook(None)
        self._stopped.set()

    async def _on_connect(self, sender: FrameSender) -> None:
        """Replay held frames onto a just-connected client, newest window first (#132).

        Runs inside the server's connect hook — after the hello, before the connection
        joins the broadcast set — so every replayed frame precedes live traffic. The
        claim marks all undelivered rows delivered, so a second reattach replays none.
        """
        if self._log is None:
            return
        for frame in await self._log.claim_replay(
            platform=CLI_PLATFORM, limit=self._replay_limit
        ):
            await sender({**frame, "replay": True})

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
        if self._log is not None:
            await self._log.record(
                platform=CLI_PLATFORM, thread_key=thread_key, role=ROLE_OWNER,
                surface=Surface.DM.value, kind=TYPE_USER, text=text, delivered=True,
            )
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
        if self._log is not None:
            await self._log.record(
                platform=CLI_PLATFORM, thread_key=thread_key, role=ROLE_OWNER,
                surface=Surface.DM.value, kind=TYPE_COMMAND,
                text=f"/{name} {arg}".strip(), delivered=True,
            )

        async def reply(reply_text: str) -> None:
            await sender(reply_frame(thread_key, reply_text))
            if self._log is not None:
                # A command's direct answer goes back to the requester, who is attached
                # by definition — never replayed, so record delivered with no payload.
                await self._log.record(
                    platform=CLI_PLATFORM, thread_key=thread_key, role=ROLE_CHIEF,
                    surface=Surface.DM.value, kind=TYPE_REPLY, text=reply_text,
                    payload=None, delivered=True,
                )

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
