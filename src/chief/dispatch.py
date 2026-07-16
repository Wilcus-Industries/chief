"""Dispatcher: routes an inbound Message through its session and back out.

Inbound order: approval answers are consumed first (a turn blocked on an
approval card would deadlock behind the session lock otherwise), strangers
are logged and dropped, owner messages go on the event bus and run a turn.
``system`` senders (monitor/cron wakes) run a turn but are never published —
that would let monitors trigger themselves.
"""

import logging
from typing import Protocol

from chief.adapters.base import Adapter, Message
from chief.agent.manager import SessionManager
from chief.approvals import ApprovalBroker
from chief.bus import Event, EventBus
from chief.strangers import StrangerLog


class CommandRunner(Protocol):
    """Slash-command hook: returns the reply, or None if not a command."""

    async def run(self, message: Message) -> str | None: ...

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
    ) -> None:
        self._manager = manager
        self._bus = bus
        self._approvals = approvals
        self._strangers = strangers
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

    async def handle(self, message: Message) -> None:
        """Run one turn for an inbound message and send the reply back."""
        if self._approvals and self._approvals.resolve(
            message.thread_key, message.text
        ):
            return
        if message.sender not in (OWNER, SYSTEM):
            if self._strangers is not None:
                await self._strangers.log(message)
            return
        if self._commands is not None:
            reply = await self._commands.run(message)
            if reply is not None:
                await self.adapter(message.channel).send(message.thread_key, reply)
                return
        if self._bus is not None and message.sender == OWNER:
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
        await self._run_turn(message)

    async def _run_turn(self, message: Message) -> None:
        adapter = self.adapter(message.channel)
        session = await self._manager.get_or_create(
            message.thread_key, message.channel
        )

        async def on_delta(text: str) -> None:
            await adapter.send_delta(message.thread_key, text)

        try:
            result = await session.run_turn(message.text, on_delta)
        except Exception:
            logger.exception("turn failed for thread %s", message.thread_key)
            await adapter.send(
                message.thread_key, "error: something went wrong running that turn"
            )
            return
        await adapter.send(message.thread_key, result.text)
        if result.notice:
            await adapter.send(message.thread_key, result.notice)
