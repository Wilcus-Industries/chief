"""Mid-turn injection inbox: owner messages offered to a running turn.

A session's inbox is open only while a turn holds its lock. The tool loop
drains it after each tool batch (and before finishing on a text-only answer),
so the agent sees the message on its very next model call. Whatever the turn
never reached — an error, the iteration limit, the turn's post-model tail — is
handed back on close through each item's ``on_leftover``, so the caller can run
it as an ordinary queued turn. Only a crash or restart in that handoff loses
one, exactly as it would a normally queued message.

The turn's context hooks (memory recall, sender gating) ran once for the
turn's own text; injected text inherits that owner context and is not
re-screened or recalled against.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

# Delivers an answer the model finished while an injected owner message was
# already waiting, before the turn continues on that message (see
# ``chief.agent.loop.run_turn``).
OnInterim = Callable[[str], Awaitable[None]]


class Injections(Protocol):
    """What the tool loop needs of an inbox (:class:`Inbox` satisfies it)."""

    def pending(self) -> bool: ...

    async def take(self) -> list[str]: ...


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

    def pending(self) -> bool:
        return bool(self._items)

    async def take(self) -> list[str]:
        """Drain every queued text, in arrival order, firing its consume hook.
        Items leave one at a time, so a raising hook leaves the rest for close."""
        texts = []
        while self._items:
            item = self._items.pop(0)
            await item.on_consumed()
            texts.append(item.text)
        return texts

    def close(self) -> None:
        """Stop accepting and hand every untaken item back for requeueing."""
        self._open = False
        items, self._items = self._items, []
        for item in items:
            item.on_leftover()
