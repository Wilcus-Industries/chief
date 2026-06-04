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
from typing import Protocol

from ..gate.approvals import ApprovalAction
from ..memory.store import Fact
from ..persistence.models import Task

ReadyHook = Callable[[], Awaitable[None]]

#: Wire prefix for approval-card button payloads, shared by every button-capable
#: adapter: ``"{CALLBACK_PREFIX}:{approval_id}:{action}"`` (Telegram ``callback_data``,
#: Discord component ``custom_id``).
CALLBACK_PREFIX = "appr"


def parse_callback(data: str) -> tuple[int, ApprovalAction] | None:
    """Decode an approval button payload, or ``None`` if it is not ours / malformed."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    try:
        return int(parts[1]), ApprovalAction(parts[2])
    except ValueError:
        return None


def split_message(text: str, limit: int) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` (platform message cap).

    Minimal hard split (M2); smart/file-aware splitting is M8.
    """
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


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


class Engine(Protocol):
    """The slice of :class:`~chief.core.tasks.TaskManager` an adapter drives."""

    async def dispatch(
        self, *, thread_key: str, text: str, is_general: bool = False
    ) -> None: ...
    async def cancel(self, thread_key: str) -> bool: ...
    async def active_tasks(self) -> list[Task]: ...


class ApprovalResolver(Protocol):
    """The slice of :class:`~chief.gate.approvals.ApprovalManager` button taps call."""

    async def resolve(
        self, approval_id: int, action: ApprovalAction, *, decided_by: str
    ) -> None: ...


class MemoryReader(Protocol):
    """The slice of :class:`~chief.memory.store.MemoryStore` the commands touch."""

    def list_facts(self, namespace: str) -> list[Fact]: ...
    async def forget(self, namespace: str, query: str) -> list[Fact]: ...


class Adapter(ABC):
    """A chat-platform connection that long-polls and routes inbound messages."""

    @abstractmethod
    async def run(self, on_ready: ReadyHook | None = None) -> None:
        """Start the platform connection and route messages until ``stop`` is set.

        Async so the adapter shares the engine's event loop (one loop drives polling,
        per-task background turns, the semaphore, and idle timers). ``on_ready`` runs
        once the connection is live (used for restart recovery, which must send).
        """
