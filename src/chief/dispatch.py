"""Dispatcher: routes an inbound Message through its session and back out.

styleguide: file-length — one cohesive turn-routing flow (approval/stranger/
command routing plus the turn's own observer-streaming callbacks); the pure
policy frame logic already lives in chief.policy. Splitting scatters one turn.

Inbound order: approval answers are consumed first (a turn blocked on an
approval card would deadlock behind the session lock otherwise); an owner
message sent as an interrupt is offered to the thread's running turn
(``try_inject``) instead of queuing behind it; strangers
are logged and published to the bus but never run a turn, owner messages go on
the event bus and run a turn. ``system`` senders (monitor/cron wakes) run a
turn but are never published — that would let monitors trigger themselves.

Every completed turn emits one coarse ``tick`` to the observer hub so the
cockpit can watch a thread it isn't tapped into (other tabs' unread/reorder/
snippet); web turns also stream their own ``delta``/``final`` through the web
adapter, so the dispatcher skips only the rich ``final`` for them. Every turn's
``inbound`` streams to its watchers when the turn begins (and an injected
message's when the loop takes it). A slash command's answer is a ``command``
frame to its watchers on every channel — for web *instead of* a ``final``, so
the client never mistakes it for a streaming reply. A client *tapped into* a
non-web thread
also gets that turn's final and — as the
thread's :class:`~chief.policy.StreamPolicy` allows (resolved once per turn from
the row override or channel default) — its delta / tool-tick / inline
tool-result frames. Via the public ``tapped()``, it also gets an out-of-turn
approval card the gate's ``approval_asker`` raises (#267).

Every owner/system message is recorded in flight (``chief.selfedit.inflight``)
from ``handle`` (or the caller's enqueue) until it is answered, so a process
death can't lose it; ``replay`` and ``hold`` serve the next boot's resume.

Non-owner output rule (PRD #308): a turn in a thread whose surface reaches a
non-owner (``audience`` says no) is *quiet* — no deltas, final text, notice,
error or command reply reaches that adapter, whoever triggered the turn (a
peer, monitor, cron, or the owner typing from the web). The hub frames still
fire, so the cockpit (always the owner) sees the turn. And only an owner's
message ever answers an approval card.
"""

import asyncio
import logging
from typing import Protocol

from chief.adapters.base import Adapter, Message, Requeue
from chief.agent.inbox import Injected
from chief.agent.manager import SessionManager
from chief.agent.restart_gate import NullTicket, TurnTicket
from chief.approvals import ApprovalBroker
from chief.bus import Event, EventBus
from chief.hub import ObserverHub
from chief.policy import (
    Audience,
    StreamPolicy,
    delta_frame,
    resolve,
    result_frame,
    thinking_frame,
    tool_frame,
)
from chief.provider.base import ProviderError, ToolCall
from chief.selfedit.recovery import RestartBoundary
from chief.strangers import StrangerLog


class Tracker(Protocol):
    """The in-flight record (``chief.selfedit.inflight.InflightLog``)."""

    def track(
        self, thread_key: str, channel: str, sender: str, text: str
    ) -> TurnTicket: ...


class CommandRunner(Protocol):
    """Slash-command hook: a string answers deterministically, a Message
    rewrites the turn (skill invocation), None means not a command."""

    async def run(self, message: Message) -> str | Message | None: ...

logger = logging.getLogger(__name__)

OWNER = "owner"
SYSTEM = "system"
WEB_CHANNEL = "web"
PREVIEW_CHARS = 120


class Dispatcher:
    """Connects adapters to sessions: one handle() per inbound message."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        bus: EventBus | None = None,
        hub: ObserverHub | None = None,
        approvals: ApprovalBroker | None = None,
        strangers: StrangerLog | None = None,
        restart: RestartBoundary | None = None,
        channel_defaults: dict[str, StreamPolicy] | None = None,
        audience: Audience | None = None,
        inflight: Tracker | None = None,
    ) -> None:
        self._manager = manager
        self._bus = bus
        self._hub = hub
        self._approvals = approvals
        self._strangers = strangers
        self._restart = restart
        self._channel_defaults = channel_defaults or {}
        self._audience: Audience = audience or (lambda _channel, _thread: True)
        self._adapters: dict[str, Adapter] = {}
        self._commands: CommandRunner | None = None
        self._inflight = inflight
        # Threads the boot is resuming: new messages wait behind the replay.
        self._held: dict[str, asyncio.Event] = {}

    def set_commands(self, commands: CommandRunner) -> None:
        """Attach the slash-command set (built after the dispatcher exists)."""
        self._commands = commands

    def register(self, adapter: Adapter) -> None:
        """Make an adapter reachable for outbound sends on its channel."""
        self._adapters[adapter.name] = adapter

    def adapter(self, channel: str) -> Adapter:
        """The adapter serving a channel (KeyError on unknown = wiring bug)."""
        return self._adapters[channel]

    def track(self, message: Message) -> TurnTicket:
        """Record a message the moment chief holds it (see
        ``chief.selfedit.inflight``), so a death before it is answered can't
        lose it. Only owner and system messages: a stranger's never runs a
        turn, so it is never replayed as one either."""
        if self._inflight is None or message.sender not in (OWNER, SYSTEM):
            return NullTicket()
        return self._inflight.track(
            message.thread_key, message.channel, message.sender, message.text
        )

    def hold(self, threads: list[str]) -> None:
        """Queue new messages on these threads behind their boot resume
        (``chief.selfedit.resume``) until ``release``."""
        for thread in threads:
            self._held.setdefault(thread, asyncio.Event())

    def release(self, thread_key: str) -> None:
        if (resumed := self._held.pop(thread_key, None)) is not None:
            resumed.set()

    def resolve_approval(self, message: Message) -> bool:
        """Consume a message as an approval answer if a card is pending on its
        thread; True means it answered one (and must not start a turn).

        A channel whose per-thread turns run on a single serialized worker
        (iMessage) calls this in its poll/route stage, *before* enqueuing the
        row — so the owner's answer bypasses the head-of-line-blocked turn it
        must unblock instead of queuing behind it (that queue-behind is the
        approval deadlock).

        Only the owner answers: a stranger's "yes" on a thread with a pending
        card (theirs, or a group's) is never consumed as an approval."""
        if self._approvals is None or message.sender != OWNER:
            return False
        return self._approvals.resolve(message.thread_key, message.text)

    def try_inject(self, message: Message, requeue: Requeue) -> bool:
        """Offer an owner message to its thread's in-flight turn instead of
        queuing it behind; True means the turn took it (or will hand it back
        through ``requeue`` if it finishes first — the caller's normal queue
        path, e.g. the iMessage FIFO). False: no turn running, a stranger, or
        a slash command (those always route through :meth:`handle`).

        The agent sees it at the turn's next tool boundary, where it streams
        as an ``inbound`` frame and is published to the bus like any owner
        message."""
        if message.sender != OWNER or message.text.startswith("/"):
            return False
        session = self._manager.peek(message.thread_key)
        if session is None:
            return False
        # Waiting in the inbox it is in no transcript yet: recorded until the
        # loop takes it, or handed back with the message to the caller's queue
        # (a cancelled turn hands it back too, so the record must go along).
        ticket = self.track(message)

        async def consumed() -> None:
            ticket.done()  # in the transcript; the turn's own record covers it
            self._inbound(message)
            await self._publish(message)

        def leftover() -> None:
            requeue(message, ticket)  # the queue path now owns the record

        if session.inject(Injected(message.text, consumed, leftover)):
            return True
        ticket.done()
        return False

    async def handle(
        self, message: Message, *, fire_restart: bool = True,
        ticket: TurnTicket | None = None,
    ) -> None:
        """Run one turn for an inbound message and send the reply back.

        A self-edit turn requests a restart mid-turn; the execv fires here,
        *after* the reply is sent, so it's never lost. ``fire_restart=False``
        defers that to the caller (imessage, which must persist its inbound
        cursor first — else the row re-polls and chief answers twice).

        ``ticket`` is the message's in-flight record when the caller made it
        (the iMessage FIFO, at enqueue); otherwise it is recorded here. Every
        outcome but a cancel (the daemon going down) ends it."""
        ticket = ticket or self.track(message)
        try:
            await self._route(message, ticket)
        except Exception:
            ticket.done()
            raise
        if fire_restart and self._restart is not None:
            await self._restart.fire_if_requested()

    async def replay(self, message: Message, ticket: TurnTicket) -> None:
        """Run a turn a death killed (``chief.selfedit.resume``). Its message
        was already approval-checked, published and command-routed before it
        was held, so only the turn itself runs again — and only ever as the
        owner or system: nothing else is recorded, but a record is a file."""
        if message.sender not in (OWNER, SYSTEM):
            logger.warning("not resuming a %r turn on %s", message.sender,
                           message.thread_key)
            ticket.done()
            return
        await self._ended(message, ticket)
        if self._restart is not None:
            await self._restart.fire_if_requested()

    async def _route(self, message: Message, ticket: TurnTicket) -> None:
        if self.resolve_approval(message):
            ticket.done()
            return
        if message.sender not in (OWNER, SYSTEM):
            if self._strangers is not None:
                await self._strangers.log(message)
            # Published (never dispatched) so monitors can implement notify
            # policy — e.g. an iMessage whitelist tier — as agent policy.
            await self._publish(message)
            ticket.done()
            return
        if (resuming := self._held.get(message.thread_key)) is not None:
            await resuming.wait()
        if self._commands is not None:
            outcome = await self._commands.run(message)
            if isinstance(outcome, str):
                await self._answer_command(message, outcome)
                ticket.done()
                return
            if isinstance(outcome, Message):
                # A replay must run the rewritten turn, not re-run the command.
                rewritten = self.track(outcome)
                ticket.done()
                message, ticket = outcome, rewritten
        if message.sender == OWNER:
            await self._publish(message)
        await self._ended(message, ticket)

    async def _ended(self, message: Message, ticket: TurnTicket) -> None:
        """Run the turn, then end its record: after the reply is sent, so a
        death mid-send still wakes the thread."""
        try:
            await self._run_turn(message, ticket)
        except Exception:
            ticket.done()
            raise
        ticket.done()

    async def _answer_command(self, message: Message, answer: str) -> None:
        """A slash command's answer: never stored, not a turn. The origin
        channel replies as before — except web, whose client gets only a
        ``command`` frame (carrying the command too), so it is never mistaken
        for the reply a running turn is streaming — and a quiet (non-owner)
        thread, where it shows only in the cockpit. A client tapped into any
        other thread gets the same frame."""
        tk = message.thread_key
        owner_surface = self._audience(message.channel, tk)
        if message.channel != WEB_CHANNEL and owner_surface:
            await self.adapter(message.channel).send(tk, answer)
        self.tapped(
            tk, {"type": "command", "thread": tk, "command": message.text,
                 "text": answer},
        )

    async def _publish(self, message: Message) -> None:
        if self._bus is None:
            return
        await self._bus.publish(
            Event(
                type="message.inbound",
                channel=message.channel,
                payload={
                    "thread_key": message.thread_key,
                    "sender": message.sender,
                    "text": message.text,
                },
            )
        )

    async def _run_turn(self, message: Message, ticket: TurnTicket) -> None:
        adapter = self.adapter(message.channel)
        session = await self._manager.get_or_create(
            message.thread_key, message.channel
        )
        tk = message.thread_key
        # Absolute: ignores the sender, so owner-from-web is quiet here too.
        quiet = not self._audience(message.channel, tk)
        override = await self._manager.stream_policy(tk)
        policy = resolve(message.channel, override, self._channel_defaults)
        # Web turns stream their own delta (adapter); for every other channel
        # tapped() re-checks the live subscription at emit time.
        non_web = message.channel != WEB_CHANNEL

        async def on_begin() -> None:
            # Every channel, web included: the client renders the owner's text
            # from this frame once the turn starts, not optimistically at send.
            self._inbound(message)

        async def on_delta(text: str) -> None:
            if not quiet:
                await adapter.send_delta(tk, text)
            if non_web and (frame := delta_frame(policy, tk, text)):
                self.tapped(tk, frame)

        async def on_interim(text: str) -> None:
            # An answer an injected message overtook still reaches the owner
            # (the cockpit only, on a quiet thread).
            if not quiet:
                await adapter.send(tk, text)
            if non_web:
                self.tapped(tk, {"type": "final", "thread": tk, "text": text})

        async def on_tool(call: ToolCall) -> None:
            # Web turns get tool ticks too — the WebAdapter never emits them.
            if frame := tool_frame(policy, tk, name=call.name, call_id=call.id):
                self.tapped(tk, frame)

        async def on_tool_result(call: ToolCall, result: str) -> None:
            if frame := result_frame(policy, tk, call_id=call.id, result=result):
                self.tapped(tk, frame)

        async def on_reasoning(text: str) -> None:
            # Web turns too: the WebAdapter only carries reply text.
            if frame := thinking_frame(policy, tk, text):
                self.tapped(tk, frame)

        try:
            result = await session.run_turn(
                message.text, on_delta, sender=message.sender,
                on_tool=on_tool, on_tool_result=on_tool_result,
                on_begin=on_begin, on_interim=on_interim,
                on_reasoning=on_reasoning, ticket=ticket,
            )
        except ProviderError as exc:
            # A backend failure is the owner's to see (e.g. proxy down, bad
            # key): surface its message so it's actionable, not a dead end.
            logger.exception("turn failed for thread %s", tk)
            await self._reply(adapter, message, f"error: {exc}", quiet)
            return
        except Exception:
            # Any other failure may carry internals — keep the generic text.
            logger.exception("turn failed for thread %s", tk)
            await self._reply(
                adapter, message,
                "error: something went wrong running that turn", quiet,
            )
            return
        if not quiet:
            await adapter.send(tk, result.text)
            if result.notice:
                await adapter.send(tk, result.notice)
        self._turn_end(message, result.text)

    async def _reply(
        self, adapter: Adapter, message: Message, text: str, quiet: bool
    ) -> None:
        if not quiet:
            await adapter.send(message.thread_key, text)
        self._turn_end(message, text)

    def _inbound(self, message: Message) -> None:
        tk = message.thread_key
        self.tapped(tk, {"type": "inbound", "thread": tk, "text": message.text})

    def tapped(self, thread_key: str, frame: dict[str, object]) -> None:
        """Deliver a rich frame only if a client is tapped into the thread."""
        if self._hub is not None and self._hub.is_watched(thread_key):
            self._hub.to_watchers(thread_key, frame)

    def _turn_end(self, message: Message, reply: str) -> None:
        # Every completed turn emits the coarse tick; only non-web threads also
        # get the tapped rich `final` (web turns stream theirs via WebAdapter).
        if self._hub is None:
            return
        tk = message.thread_key
        if message.channel != WEB_CHANNEL:
            self.tapped(tk, {"type": "final", "thread": tk, "text": reply})
        self._hub.tick(tk, message.channel, reply[:PREVIEW_CHARS])
