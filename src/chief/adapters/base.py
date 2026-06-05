"""Platform-neutral adapter interface and message types.

Each chat platform implements :class:`Adapter`; the rest of the system speaks only in
:class:`Message` and :class:`Tier`. Tier is decided by the *sender's* id (exact owner-id
match, no fuzzy matching — DESIGN: Identity & access). Outbound replies go through the
engine's :class:`~chief.core.tasks.TaskIO`, which the adapter implements.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..gate.approvals import ApprovalAction
from ..memory.store import Fact
from ..persistence import contacts as contact_repo
from ..persistence.models import Contact, Task
from ..persistence.rate_limits import check_and_increment

ReadyHook = Callable[[], Awaitable[None]]

#: Wire prefix for approval-card button payloads, shared by every button-capable
#: adapter: ``"{CALLBACK_PREFIX}:{approval_id}:{action}"`` (Telegram ``callback_data``,
#: Discord component ``custom_id``).
CALLBACK_PREFIX = "appr"

#: Wire prefix for the guest-admission card buttons (M6). Like the approval prefix but
#: self-contained — the payload carries the contact id + decision, so a tap resolves
#: with no live in-memory state (restart-proof, no re-arm needed).
ADMISSION_PREFIX = "adm"


def parse_callback(data: str) -> tuple[int, ApprovalAction] | None:
    """Decode an approval button payload, or ``None`` if it is not ours / malformed."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    try:
        return int(parts[1]), ApprovalAction(parts[2])
    except ValueError:
        return None


class AdmissionAction(Enum):
    """A first-contact admission decision. Values double as the button payload token."""

    ADMIT = "admit"
    BLOCK = "block"


@dataclass(frozen=True)
class AdmissionCard:
    """A first-contact prompt posted to the Front Desk: admit this guest, or block."""

    contact_id: int
    text: str


def admission_payload(contact_id: int, action: AdmissionAction) -> str:
    """The button payload for an admission decision (``adm:{contact_id}:{action}``)."""
    return f"{ADMISSION_PREFIX}:{contact_id}:{action.value}"


def parse_admission(data: str) -> tuple[int, AdmissionAction] | None:
    """Decode an admission button payload, or ``None`` if not ours / malformed."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != ADMISSION_PREFIX:
        return None
    try:
        return int(parts[1]), AdmissionAction(parts[2])
    except ValueError:
        return None


async def apply_admission(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    contact_id: int,
    action: AdmissionAction,
) -> Contact | None:
    """Persist an admission decision; return the updated contact (``None`` if gone)."""
    async with session_factory() as session:
        contact = await session.get(Contact, contact_id)
        if contact is None:
            return None
        state = (
            contact_repo.STATE_ADMITTED
            if action is AdmissionAction.ADMIT
            else contact_repo.STATE_BLOCKED
        )
        await contact_repo.set_contact_state(session, contact, state)
        return contact


class GuestAction(Enum):
    """What the adapter does with a guest message (decided by :func:`decide_guest`)."""

    IGNORE = "ignore"  # blocked sender — drop silently, no reply, no relay
    DROP = "drop"  # over a rate limit — drop (maybe notify the owner on global trip)
    RELAY = "relay"  # muted — relay to the Front Desk silently, no reply
    ADMIT = "admit"  # pending first contact — relay + admission card + holding ack
    DISPATCH = "dispatch"  # admitted — run the full guest receptionist session


@dataclass(frozen=True)
class GuestDecision:
    """The outcome of the guest gate: an action plus the bits the adapter needs."""

    action: GuestAction
    contact_id: int
    over_global: bool = False


async def decide_guest(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    message: "Message",
    rate_limit: int,
    rate_window_seconds: int,
    global_limit: int,
) -> GuestDecision:
    """Gate a guest message: block → rate limit → mute → admission → dispatch.

    Order is cheapest/most-restrictive first. The rate check runs for pending and
    admitted guests alike (a spammer can't flood the admission card either), but only
    after the block check (a blocked sender costs nothing). The contact already exists —
    the adapter records it before gating — so a missing row defaults to pending.
    """
    async with session_factory() as session:
        contact = await contact_repo.get_contact(
            session, platform=message.platform, user_id=str(message.sender_id)
        )
        contact_id = contact.id if contact is not None else 0
        state = contact.state if contact is not None else contact_repo.STATE_PENDING
        namespace = (
            contact.namespace
            if contact is not None
            else f"{message.platform}:{message.sender_id}"
        )
        if state == contact_repo.STATE_BLOCKED:
            return GuestDecision(GuestAction.IGNORE, contact_id)
        if not await check_and_increment(
            session,
            scope=namespace,
            window_seconds=rate_window_seconds,
            limit=rate_limit,
        ):
            return GuestDecision(GuestAction.DROP, contact_id)
        if not await check_and_increment(
            session,
            scope="global",
            window_seconds=rate_window_seconds,
            limit=global_limit,
        ):
            return GuestDecision(GuestAction.DROP, contact_id, over_global=True)
        if state == contact_repo.STATE_MUTED:
            return GuestDecision(GuestAction.RELAY, contact_id)
        if state == contact_repo.STATE_ADMITTED:
            return GuestDecision(GuestAction.DISPATCH, contact_id)
        return GuestDecision(GuestAction.ADMIT, contact_id)


ReplyFn = Callable[[str], Awaitable[None]]


class GuestIO(Protocol):
    """The slice of a platform IO the guest gate sends through (Front Desk output)."""

    async def send(self, thread_key: str, text: str) -> None: ...
    async def send_admission_card(
        self, route: str, card: AdmissionCard
    ) -> None: ...


async def handle_guest_message(
    *,
    message: "Message",
    io: GuestIO,
    engine: "Engine",
    session_factory: async_sessionmaker[AsyncSession],
    reply: ReplyFn,
    front_desk: str,
    guest_ack: str,
    rate_limit: int,
    rate_window_seconds: int,
    global_limit: int,
    prompted: set[int],
) -> None:
    """Run the platform-neutral guest gate: block / rate / mute / admit / dispatch (M6).

    Shared by both adapters so the receptionist policy lives in one place. ``reply``
    sends the holding ack back to the guest; ``prompted`` dedupes the admission card so
    a pending guest who keeps messaging doesn't re-card the owner (notes still relay).
    """
    decision = await decide_guest(
        session_factory,
        message=message,
        rate_limit=rate_limit,
        rate_window_seconds=rate_window_seconds,
        global_limit=global_limit,
    )
    label = message.sender_name or "a visitor"
    if decision.action is GuestAction.IGNORE:
        return
    if decision.action is GuestAction.DROP:
        if decision.over_global:
            await io.send(
                front_desk,
                "⚠️ Guests are hitting the global rate limit; new messages are "
                "being held.",
            )
        return
    if decision.action is GuestAction.RELAY:  # muted: take the message silently
        await io.send(front_desk, f"📨 {label} (muted):\n\n{message.text}")
        return
    if decision.action is GuestAction.ADMIT:  # pending first contact
        await io.send(front_desk, f"📨 Message from {label}:\n\n{message.text}")
        if decision.contact_id not in prompted:
            prompted.add(decision.contact_id)
            await io.send_admission_card(
                front_desk,
                AdmissionCard(
                    contact_id=decision.contact_id,
                    text=f"🔔 New contact: {label}. Admit to the receptionist, "
                    "or block?",
                ),
            )
        await reply(guest_ack)
        return
    await engine.dispatch_guest(  # admitted
        thread_key=message.thread_key,
        text=message.text,
        from_label=message.sender_name,
    )


def default_branch_title() -> str:
    """A timestamped default title for a ``/branch`` invoked with no argument."""
    return "Branched chat " + datetime.now(UTC).strftime("%m-%d %H:%M")


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


#: Inbound media caps (owner-only image/PDF intake, M8) — bound the bytes a single
#: turn pulls into memory. A message over either cap drops the offending file silently.
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MB


def is_supported_media(media_type: str) -> bool:
    """True for media chief ingests natively — images and PDFs (DESIGN M8: no OCR)."""
    base = media_type.split(";")[0].strip().lower()
    return base.startswith("image/") or base == "application/pdf"


@dataclass(frozen=True)
class Attachment:
    """An inbound binary file (an owner image or PDF) carried with a message.

    ``data`` is the raw bytes; the model sees them natively (vision / PDF), so there is
    no text-extraction step. Kept frozen so :class:`Message` stays hashable.
    """

    media_type: str
    data: bytes
    filename: str | None = None


@dataclass(frozen=True)
class Message:
    """A normalized inbound message from any platform."""

    platform: str
    sender_id: int
    text: str
    thread_key: str
    tier: Tier
    sender_name: str | None = None
    #: Owner-only image/PDF files (M8). A tuple keeps the dataclass frozen/hashable.
    attachments: tuple[Attachment, ...] = ()


class Engine(Protocol):
    """The slice of :class:`~chief.core.tasks.TaskManager` an adapter drives."""

    async def dispatch(
        self,
        *,
        thread_key: str,
        text: str,
        attachments: tuple[Attachment, ...] = (),
        is_general: bool = False,
    ) -> None: ...
    async def dispatch_guest(
        self, *, thread_key: str, text: str, from_label: str | None = None
    ) -> None: ...
    async def cancel(self, thread_key: str) -> bool: ...
    async def active_tasks(self) -> list[Task]: ...
    async def branch(self, thread_key: str, title: str) -> str: ...


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

    @abstractmethod
    async def stop(self) -> None:
        """Close the platform connection so :meth:`run` returns (shutdown path)."""
