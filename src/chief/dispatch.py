"""Dispatcher: routes an inbound Message through its session and back out.

styleguide: file-length — one cohesive turn-routing flow (approval/stranger/
command routing plus the turn's own observer-streaming callbacks); the pure
policy frame logic already lives in chief.policy. Splitting scatters one turn.

Inbound order: approval answers are consumed first (a turn blocked on an
approval card would deadlock behind the session lock otherwise), strangers
are logged and published to the bus but never run a turn, owner messages go on
the event bus and run a turn. ``system`` senders (monitor/cron wakes) run a
turn but are never published — that would let monitors trigger themselves.

Every completed turn emits one coarse ``tick`` to the observer hub; web turns
stream their own ``delta``/``final`` via the adapter, so the dispatcher skips
their rich ``final``. A client *tapped into* a non-web thread also gets that
turn's inbound/final and — as the thread's :class:`~chief.policy.StreamPolicy`
allows (resolved once per turn from the row override or channel default) — its
delta / tool-tick / inline tool-result frames.
"""

import logging
from typing import Protocol

from chief.adapters.base import Adapter, Message
from chief.agent.manager import SessionManager
from chief.approvals import ApprovalBroker
from chief.bus import Event, EventBus
from chief.hub import ObserverHub
from chief.policy import StreamPolicy, delta_frame, resolve, result_frame, tool_frame
from chief.provider.base import ProviderError, ToolCall
from chief.selfedit.recovery import RestartBoundary
from chief.strangers import StrangerLog


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
    ) -> None:
        self._manager = manager
        self._bus = bus
        self._hub = hub
        self._approvals = approvals
        self._strangers = strangers
        self._restart = restart
        self._channel_defaults = channel_defaults or {}
        self._adapters: dict[str, Adapter] = {}
        self._commands: CommandRunner | None = None

    def set_commands(self, commands: CommandRunner) -> None:
        """Attach the slash-command set (built after the dispatcher exists)."""
        self._commands = commands

    def register(self, adapter: Adapter) -> None:
        """Make an adapter reachable for outbound sends on its channel."""
        self._adapters[adapter.name] = adapter

    def adapter(self, channel: str) -> Adapter:
        """The adapter serving a channel (KeyError on unknown = wiring bug)."""
        return self._adapters[channel]

    def resolve_approval(self, message: Message) -> bool:
        """Consume a message as an approval answer if a card is pending on its
        thread; True means it answered one (and must not start a turn).

        A channel whose per-thread turns run on a single serialized worker
        (iMessage) calls this in its poll/route stage, *before* enqueuing the
        row — so the owner's answer bypasses the head-of-line-blocked turn it
        must unblock instead of queuing behind it (that queue-behind is the
        approval deadlock)."""
        if self._approvals is None:
            return False
        return self._approvals.resolve(message.thread_key, message.text)

    async def handle(self, message: Message, *, fire_restart: bool = True) -> None:
        """Run one turn for an inbound message and send the reply back.

        A self-edit turn requests a restart mid-turn; the execv fires here,
        *after* the reply is sent, so it's never lost. ``fire_restart=False``
        defers that to the caller (imessage, which must persist its inbound
        cursor first — else the row re-polls and chief answers twice)."""
        if self.resolve_approval(message):
            return
        if message.sender not in (OWNER, SYSTEM):
            if self._strangers is not None:
                await self._strangers.log(message)
            # Published (never dispatched) so monitors can implement notify
            # policy — e.g. an iMessage whitelist tier — as agent policy.
            await self._publish(message)
            return
        if self._commands is not None:
            outcome = await self._commands.run(message)
            if isinstance(outcome, str):
                await self.adapter(message.channel).send(message.thread_key, outcome)
                return
            if isinstance(outcome, Message):
                message = outcome
        if message.sender == OWNER:
            await self._publish(message)
        await self._run_turn(message)
        if fire_restart and self._restart is not None:
            await self._restart.fire_if_requested()

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

    async def _run_turn(self, message: Message) -> None:
        adapter = self.adapter(message.channel)
        session = await self._manager.get_or_create(
            message.thread_key, message.channel
        )
        tk = message.thread_key
        override = await self._manager.stream_policy(tk)
        policy = resolve(message.channel, override, self._channel_defaults)
        # Web turns stream their own inbound/delta (adapter + JS); for every
        # other channel _tapped re-checks the live subscription at emit time.
        non_web = message.channel != WEB_CHANNEL
        if non_web:
            self._tapped(tk, {"type": "inbound", "thread": tk, "text": message.text})

        async def on_delta(text: str) -> None:
            await adapter.send_delta(tk, text)
            if non_web and (frame := delta_frame(policy, tk, text)):
                self._tapped(tk, frame)

        async def on_tool(call: ToolCall) -> None:
            # Web turns get tool ticks too — the WebAdapter never emits them.
            if frame := tool_frame(policy, tk, name=call.name, call_id=call.id):
                self._tapped(tk, frame)

        async def on_tool_result(call: ToolCall, result: str) -> None:
            if frame := result_frame(policy, tk, call_id=call.id, result=result):
                self._tapped(tk, frame)

        try:
            result = await session.run_turn(
                message.text, on_delta, sender=message.sender,
                on_tool=on_tool, on_tool_result=on_tool_result,
            )
        except ProviderError as exc:
            # A backend failure is the owner's to see (e.g. proxy down, bad
            # key): surface its message so it's actionable, not a dead end.
            logger.exception("turn failed for thread %s", tk)
            await self._reply(adapter, message, f"error: {exc}")
            return
        except Exception:
            # Any other failure may carry internals — keep the generic text.
            logger.exception("turn failed for thread %s", tk)
            await self._reply(
                adapter, message, "error: something went wrong running that turn"
            )
            return
        await adapter.send(tk, result.text)
        if result.notice:
            await adapter.send(tk, result.notice)
        self._turn_end(message, result.text)

    async def _reply(
        self, adapter: Adapter, message: Message, text: str
    ) -> None:
        await adapter.send(message.thread_key, text)
        self._turn_end(message, text)

    def _tapped(self, thread_key: str, frame: dict[str, object]) -> None:
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
            self._tapped(tk, {"type": "final", "thread": tk, "text": reply})
        self._hub.tick(tk, message.channel, reply[:PREVIEW_CHARS])
