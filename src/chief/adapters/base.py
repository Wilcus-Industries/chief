"""Platform-neutral adapter interface and message types.

Each chat platform implements :class:`Adapter`; the rest of the system speaks only in
:class:`Message`/:class:`Reply` and :class:`Tier`. Tier is decided by the *sender's* id
(exact owner-id match, no fuzzy matching — DESIGN: Identity & access).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


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


@dataclass(frozen=True)
class Reply:
    """A normalized outbound reply."""

    text: str


class Adapter(ABC):
    """A chat-platform connection that long-polls and routes inbound messages."""

    @abstractmethod
    def run(self) -> None:
        """Start the platform connection and block, routing messages until stopped.

        Synchronous because long-poll clients (python-telegram-bot ``run_polling``)
        own their event loop; one-time async setup runs before this is called.
        """
