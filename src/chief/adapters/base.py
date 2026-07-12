"""Platform-neutral adapter interface and message types.

Each chat platform implements :class:`Adapter`; the rest of the system speaks only in
:class:`Message` and :class:`Tier`. Tier is decided by the *sender's* id (exact owner-id
match, no fuzzy matching — DESIGN: Identity & access). Outbound replies go through the
engine's :class:`~chief.core.tasks.TaskIO`, which the adapter implements.
"""

import re
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
from ..persistence import usage
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

#: Wire prefix for the budget choice-card buttons (M9). Self-contained like admission:
#: the payload carries the billing cycle + decision, so a tap flips the persisted mode
#: with no parked future — restart-proof.
BUDGET_PREFIX = "bud"


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


class BudgetAction(Enum):
    """An owner budget-card choice. Values double as the button payload token."""

    DOWNGRADE = "downgrade"  # keep running, but on the cheaper model
    CONTINUE = "continue"  # keep full-quality despite (near-)exhaustion
    OVERFLOW = "overflow"  # approve pay-as-you-go spend past the credit


#: Maps a card choice to the ``(currency, mode)`` it flips (#84, #97). The card is only
#: ever posted when the Copilot **premium-request** currency is exhausted
#: (``ACTION_PAUSE`` → paused), and ``TaskManager._budget_admits`` gates owner on *that*
#: currency — so every choice must move it out of ``paused`` or the owner stays gated
#: for the rest of the cycle. Downgrade flips it to ``downgraded`` (turns resume on the
#: cheaper Copilot ``auto`` class — ~0.25× premium per turn, spike #74); Continue keeps
#: full quality; Overflow approves spend past the cap. (Pre-#97, Downgrade flipped the
#: unrelated OpenRouter dollar currency and left premium ``paused`` — owner stuck.)
_BUDGET_DECISION = {
    BudgetAction.DOWNGRADE: (usage.PREMIUM_REQUESTS, usage.MODE_DOWNGRADED),
    BudgetAction.CONTINUE: (usage.PREMIUM_REQUESTS, usage.MODE_CONTINUE),
    BudgetAction.OVERFLOW: (usage.PREMIUM_REQUESTS, usage.MODE_OVERFLOW),
}

#: Button labels for the choice card, shared by both adapters (each builds its own
#: platform widget around them, so the wording lives in one place).
BUDGET_BUTTON_LABELS = {
    BudgetAction.DOWNGRADE: "⚡ Downgrade",
    BudgetAction.CONTINUE: "▶️ Continue",
    BudgetAction.OVERFLOW: "💳 Overflow",
}

#: Card-edit text confirming an applied choice (replaces the buttons after a tap).
_BUDGET_OUTCOME = {
    BudgetAction.DOWNGRADE: "⚡ Resumed on the budget model for this cycle.",
    BudgetAction.CONTINUE: "▶️ Continuing at full quality despite the budget.",
    BudgetAction.OVERFLOW: "💳 Approved pay-as-you-go overflow for this cycle.",
}


def budget_outcome_text(action: BudgetAction) -> str:
    """The card-edit text confirming an applied budget choice."""
    return _BUDGET_OUTCOME[action]


@dataclass(frozen=True)
class BudgetCard:
    """A budget choice posted to the owner inbox when the cycle hits exhaustion."""

    cycle: str
    text: str


def budget_payload(cycle: str, action: BudgetAction) -> str:
    """The button payload for a budget choice (``bud:{cycle}:{action}``)."""
    return f"{BUDGET_PREFIX}:{cycle}:{action.value}"


def parse_budget(data: str) -> tuple[str, BudgetAction] | None:
    """Decode a budget button payload, or ``None`` if not ours / malformed.

    The cycle key (``"2026-06"``) carries no ``":"``, so a 3-field split is exact.
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != BUDGET_PREFIX:
        return None
    try:
        return parts[1], BudgetAction(parts[2])
    except ValueError:
        return None


async def apply_budget_decision(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cycle: str,
    action: BudgetAction,
) -> str:
    """Flip the currency the owner's choice targets into its mode; return it (#84)."""
    currency, mode = _BUDGET_DECISION[action]
    async with session_factory() as session:
        await usage.set_mode(session, cycle=cycle, currency=currency, mode=mode)
    return mode


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


#: Matches a Markdown fenced code block (``` … ```), spanning newlines. Such blocks are
#: kept whole when splitting so a code sample never breaks across two messages.
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

#: Boundary hierarchy for :func:`split_message`: paragraphs, then lines, then words,
#: then (last resort) a hard character cut.
_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", " ", "")

#: A reply longer than ``limit × this`` is delivered as a file, not many messages.
FILE_THRESHOLD_FACTOR = 4

#: The note posted alongside a reply sent as a file attachment.
FILE_REPLY_NOTE = "📄 Full reply attached."


def _hard_split(text: str, limit: int) -> list[str]:
    """Last-resort fixed-width slice (an oversized word or un-fenceable code block)."""
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _fenced_segments(text: str) -> list[tuple[bool, str]]:
    """Split ``text`` into ``(is_code, part)`` parts that concatenate back to ``text``.

    Code parts are whole fenced blocks (atomic — never split inside); everything else is
    a regular-text part split further on paragraph/line/word boundaries.
    """
    segments: list[tuple[bool, str]] = []
    pos = 0
    for match in _CODE_FENCE_RE.finditer(text):
        if match.start() > pos:
            segments.append((False, text[pos : match.start()]))
        segments.append((True, match.group()))
        pos = match.end()
    if pos < len(text):
        segments.append((False, text[pos:]))
    return segments


def _greedy_split(text: str, limit: int, seps: tuple[str, ...]) -> list[str]:
    """Pack ``text`` into ≤ ``limit`` chunks, breaking on the coarsest fitting sep."""
    if len(text) <= limit:
        return [text]
    sep, rest = seps[0], seps[1:]
    if sep == "":
        return _hard_split(text, limit)
    chunks: list[str] = []
    current = ""
    for piece in text.split(sep):
        candidate = current + sep + piece if current else piece
        if len(candidate) <= limit:
            current = candidate
        elif len(piece) <= limit:
            if current:
                chunks.append(current)
            current = piece
        else:  # a single piece overflows — recurse onto finer separators
            if current:
                chunks.append(current)
            sub = _greedy_split(piece, limit, rest)
            chunks.extend(sub[:-1])
            current = sub[-1]
    if current:
        chunks.append(current)
    return chunks


def split_message(text: str, limit: int) -> list[str]:
    """Split ``text`` into chunks ≤ ``limit`` on paragraph/line/word breaks (M8).

    Fenced ``` code blocks ``` stay intact (never split mid-block) when a block fits in
    ``limit``; an oversized block is char-split only as a last resort — the engine sends
    such replies as a file instead (see :func:`should_send_as_file`).
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for is_code, segment in _fenced_segments(text):
        if is_code:
            pieces = [segment] if len(segment) <= limit else _hard_split(segment, limit)
        else:
            pieces = _greedy_split(segment, limit, _SEPARATORS)
        for piece in pieces:
            if current and len(current) + len(piece) <= limit:
                current += piece
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def should_send_as_file(text: str, limit: int) -> bool:
    """True if ``text`` is better delivered as a file than as split messages (M8).

    Two deterministic triggers: a very long reply (over ``limit ×
    FILE_THRESHOLD_FACTOR``), or a fenced code block that alone exceeds ``limit`` and so
    can't be split without breaking the fence. No model round-trip.
    """
    if len(text) > limit * FILE_THRESHOLD_FACTOR:
        return True
    return any(len(m.group()) > limit for m in _CODE_FENCE_RE.finditer(text))


def reply_filename() -> str:
    """A timestamped Markdown filename for a long reply delivered as a file (M8)."""
    return "reply-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + ".md"


class Tier(Enum):
    """Caller privilege level. Values double as the persisted ``tier`` string."""

    OWNER = "owner"
    GUEST = "guest"


def classify_tier(*, sender_id: int, owner_id: int) -> Tier:
    """Return OWNER iff the sender id exactly matches the configured owner id."""
    return Tier.OWNER if sender_id == owner_id else Tier.GUEST


class Surface(Enum):
    """Where a message lives, orthogonal to :class:`Tier` (M11 group chats).

    ``HOME`` is the owner's private forum/server (topics = tasks); ``DM`` is a 1:1 chat
    (flat); ``GROUP`` is any *other* multi-party chat chief is invited to — there it
    reads every message ambiently but answers only when engaged (:func:`is_engaged`).
    Values double as the persisted string.
    """

    HOME = "home"
    DM = "dm"
    GROUP = "group"


def is_engaged(*, mentioned: bool, replied_to_bot: bool) -> bool:
    """True iff a group message addresses chief: an @mention or a reply to its message.

    Platform-neutral so both adapters share one gating rule (mirrors
    :func:`classify_tier`). Each adapter computes the two booleans from its own native
    fields; in a ``GROUP`` a non-engaged message is buffered ambiently, never answered.
    """
    return mentioned or replied_to_bot


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
    #: Which surface the message arrived on (M11). Defaults to ``DM`` so 1:1 build
    #: sites stay valid; adapters set it explicitly once group classification is wired.
    surface: Surface = Surface.DM


class Engine(Protocol):
    """The slice of :class:`~chief.core.tasks.TaskManager` an adapter drives."""

    async def dispatch(
        self,
        *,
        thread_key: str,
        text: str,
        attachments: tuple[Attachment, ...] = (),
        is_general: bool = False,
        surface: Surface = Surface.DM,
    ) -> None: ...
    async def dispatch_guest(
        self,
        *,
        thread_key: str,
        text: str,
        from_label: str | None = None,
        surface: Surface = Surface.DM,
    ) -> None: ...
    async def observe(
        self, *, thread_key: str, text: str, sender_name: str | None = None
    ) -> None: ...
    async def cancel(self, thread_key: str) -> bool: ...
    async def active_tasks(self) -> list[Task]: ...
    async def branch(self, thread_key: str, title: str) -> str: ...
    async def escalate(self, thread_key: str) -> str: ...
    async def revert(self, thread_key: str) -> str: ...
    async def route(self, thread_key: str, category: str) -> str: ...
    async def downgrade_live_sessions(self) -> None: ...
    async def composed_skills(self) -> list[str]: ...


class ApprovalResolver(Protocol):
    """The slice of the approval machinery a button tap / answer frame calls.

    Satisfied by both :class:`~chief.gate.approvals.ApprovalManager` (chat buttons) and
    :class:`~chief.gate.approvals.ApprovalRegistry` (the #136 socket answer router).
    Returns ``False`` when the approval was unknown or already decided.
    """

    async def resolve(
        self, approval_id: int, action: ApprovalAction, *, decided_by: str
    ) -> bool: ...


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
