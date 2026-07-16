"""The event bus: adapters publish every inbound event; monitors subscribe."""

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    """One event on the bus, e.g. an inbound channel message."""

    type: str
    channel: str
    payload: dict[str, Any]


EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """In-process pub/sub. A failing handler never blocks the others."""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Register a handler; returns an unsubscribe callable."""
        self._handlers.append(handler)

        def unsubscribe() -> None:
            self._handlers.remove(handler)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        """Deliver an event to every subscriber, isolating their failures."""
        for handler in list(self._handlers):
            try:
                await handler(event)
            except Exception:
                logger.exception("event handler failed for %s", event.type)
