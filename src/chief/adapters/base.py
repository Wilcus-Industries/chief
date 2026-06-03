"""Platform-neutral adapter interface and message types.

Each chat platform implements :class:`Adapter`; the rest of the system speaks only in
:class:`Message` and :class:`Tier`. Tier is decided by the *sender's* id (exact owner-id
match, no fuzzy matching — DESIGN: Identity & access). Outbound replies go through the
engine's :class:`~chief.core.tasks.TaskIO`, which the adapter implements.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

ReadyHook = Callable[[], Awaitable[None]]


class Tier(Enum):
    """Caller privilege level. Values double as the persisted ``tier`` string."""

    OWNER = "owner"
    GUEST = "guest"


def classify_tier(*, sender_id: int, owner_id: int) -> Tier:
    """Return OWNER iff the sender id exactly matches the configured owner id."""
    return Tier.OWNER if sender_id == owner_id else Tier.GUEST


@dataclass(frozen=True)
class Message:
    """A normalized inbound message from any platform."""

    platform: str
    sender_id: int
    text: str
    thread_key: str
    tier: Tier
    sender_name: str | None = None


class Adapter(ABC):
    """A chat-platform connection that long-polls and routes inbound messages."""

    @abstractmethod
    async def run(self, on_ready: ReadyHook | None = None) -> None:
        """Start the platform connection and route messages until ``stop`` is set.

        Async so the adapter shares the engine's event loop (one loop drives polling,
        per-task background turns, the semaphore, and idle timers). ``on_ready`` runs
        once the connection is live (used for restart recovery, which must send).
        """
