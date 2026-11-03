"""Mid-turn injection inbox: owner messages offered to a running turn.

A session's inbox is open only while a turn holds its lock. The tool loop
drains it at each tool boundary, so the agent sees the message on its very
next model call. Whatever the turn never reached (a text-only finish, an
error) is handed back on close through each item's ``on_leftover``, so the
caller can run it as an ordinary queued turn — an offer is never lost.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Injected:
    """One offered message: its text, an async hook fired as the loop takes it
    (stream the inbound frame, publish to the bus), and a sync hook to requeue
    it when the turn ends first."""

    text: str
    on_consumed: Callable[[], Awaitable[None]]
    on_leftover: Callable[[], None]


class Inbox:
    """Offers accepted only between ``open`` and ``close``."""

    def __init__(self) -> None:
        self._open = False
        self._items: list[Injected] = []

    def open(self) -> None:
        self._open = True

    def offer(self, item: Injected) -> bool:
        """Queue ``item`` for the running turn; False when no turn is running."""
        if not self._open:
            return False
        self._items.append(item)
        return True

    async def take(self) -> list[str]:
        """Drain every queued text, in arrival order, firing its consume hook."""
        items, self._items = self._items, []
        for item in items:
            await item.on_consumed()
        return [item.text for item in items]

    def close(self) -> None:
        """Stop accepting and hand every untaken item back for requeueing."""
        self._open = False
        items, self._items = self._items, []
        for item in items:
            item.on_leftover()
