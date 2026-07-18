"""Dispatcher: routes an inbound Message through its session and back out.

Inbound order: approval answers are consumed first (a turn blocked on an
approval card would deadlock behind the session lock otherwise), strangers
are logged and published to the bus but never run a turn, owner messages
go on the event bus and run a turn.
``system`` senders (monitor/cron wakes) run a turn but are never published —
that would let monitors trigger themselves.
"""

import logging
from typing import Protocol

from chief.adapters.base import Adapter, Message
from chief.agent.manager import SessionManager
from chief.approvals import ApprovalBroker
from chief.bus import Event, EventBus
from chief.provider.base import ProviderError
from chief.selfedit.recovery import RestartBoundary
from chief.strangers import StrangerLog


class CommandRunner(Protocol):
    """Slash-command hook: a string answers deterministically, a Message
    rewrites the turn (skill invocation), None means not a command."""

    async def run(self, message: Message) -> str | Message | None: ...

logger = logging.getLogger(__name__)

OWNER = "owner"
SYSTEM = "system"


class Dispatcher:
    """Connects adapters to sessions: one handle() per inbound message."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        bus: EventBus | None = None,
        approvals: ApprovalBroker | None = None,
        strangers: StrangerLog | None = None,
        restart: RestartBoundary | None = None,
    ) -> None:
        self._manager = manager
        self._bus = bus
        self._approvals = approvals
        self._strangers = strangers
        self._restart = restart
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

    async def handle(self, message: Message, *, fire_restart: bool = True) -> None:
        """Run one turn for an inbound message and send the reply back.

        A self-edit turn requests a restart mid-turn; the execv fires here,
        *after* the reply is sent, so it's never lost. ``fire_restart=False``
        defers that to the caller (imessage, which must persist its inbound
        cursor first — else the row re-polls and chief answers twice)."""
        if self._approvals and self._approvals.resolve(
            message.thread_key, message.text
        ):
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

        async def on_delta(text: str) -> None:
            await adapter.send_delta(message.thread_key, text)

        try:
            result = await session.run_turn(message.text, on_delta)
        except ProviderError as exc:
            # A backend failure is the owner's to see (e.g. proxy down, bad
            # key): surface its message so it's actionable, not a dead end.
            logger.exception("turn failed for thread %s", message.thread_key)
            await adapter.send(message.thread_key, f"error: {exc}")
            return
        except Exception:
            # Any other failure may carry internals — keep the generic text.
            logger.exception("turn failed for thread %s", message.thread_key)
            await adapter.send(
                message.thread_key, "error: something went wrong running that turn"
            )
            return
        await adapter.send(message.thread_key, result.text)
        if result.notice:
            await adapter.send(message.thread_key, result.notice)
