"""Dispatcher: routes an inbound Message through its session and back out."""

import logging

from chief.adapters.base import Adapter, Message
from chief.agent.manager import SessionManager

logger = logging.getLogger(__name__)


class Dispatcher:
    """Connects adapters to sessions: one handle() per inbound message."""

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> None:
        """Make an adapter reachable for outbound sends on its channel."""
        self._adapters[adapter.name] = adapter

    async def handle(self, message: Message) -> None:
        """Run one turn for an inbound message and send the reply back."""
        adapter = self._adapters[message.channel]
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
