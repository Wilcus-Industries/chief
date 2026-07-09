"""SQLAlchemy models — the full chief schema (DESIGN: Data model & persistence).

All tables are created up front; only ``contacts`` is read/written in M0/M1. The rest
(``tasks``, ``approvals``, ``policy``, ``rate_limits``, ``schedules``) are shells their
owning milestones (M2/M3/M6/M9) wire behavior onto. ``tier`` is stored as a plain string
so persistence stays independent of the adapter layer.
"""

from datetime import UTC, datetime

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all chief tables."""


class Contact(Base):
    """A known sender, used for tier/admission state and memory namespacing."""

    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("platform", "user_id", name="uq_contact_platform_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str]
    user_id: Mapped[str]
    display_name: Mapped[str | None]
    tier: Mapped[str]
    admitted: Mapped[bool] = mapped_column(default=False)
    # Admission/abuse state (M6): pending → admitted, or blocked/muted by the owner.
    # ``admitted`` is kept for back-compat; ``state`` is authoritative.
    state: Mapped[str] = mapped_column(default="pending")
    namespace: Mapped[str]
    first_seen: Mapped[datetime] = mapped_column(default=_utcnow)


class Task(Base):
    """One conversation = one task/session (lifecycle owned by M2)."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("platform", "thread_key", name="uq_task_platform_thread"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str]
    thread_key: Mapped[str]
    tier: Mapped[str]
    subject_id: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="open")
    model: Mapped[str | None]
    title: Mapped[str | None]
    sdk_session_id: Mapped[str | None]
    #: Per-thread active Google account label (issue #45). None = no account pinned.
    active_account: Mapped[str | None]
    #: Per-task ``/route`` category override (#79). None = auto-classify at spawn.
    route_category: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class Approval(Base):
    """A pending or decided permission-gate request (owned by M3)."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    kind: Mapped[str]
    payload_preview: Mapped[str | None]
    state: Mapped[str] = mapped_column(default="requested")
    decided_by: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    decided_at: Mapped[datetime | None]


class PolicyEntry(Base):
    """A NEVER/APPROVED allowlist entry (safe-matched; owned by M3)."""

    __tablename__ = "policy"

    id: Mapped[int] = mapped_column(primary_key=True)
    list_name: Mapped[str]  # "NEVER" | "APPROVED"
    tool: Mapped[str]
    arg_pattern: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class Route(Base):
    """One job category → routing target (owned by #79, part of #72).

    The row *set* is the category set: every category with a row maps to a
    ``{target_class, model}`` target — ``copilot`` (Copilot quota, e.g. ``auto``) or
    ``openrouter`` (a BYOK per-model target). Seeded from ``config.yaml`` on boot
    (mirrors :class:`PolicyEntry`); the self-config slice later makes both editable.
    ``category`` is unique so a seed / edit upserts one target per category.
    """

    __tablename__ = "routes"
    __table_args__ = (UniqueConstraint("category", name="uq_route_category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str]
    target_class: Mapped[str]  # "copilot" | "openrouter"
    model: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class RateLimit(Base):
    """Per-guest and global rate-limit counters (owned by M6)."""

    __tablename__ = "rate_limits"
    # One row per scope: the counter is read-modify-write, so the constraint stops a
    # racing insert from creating a duplicate row (which would later break the
    # single-row read). check_and_increment also serializes under a lock.
    __table_args__ = (UniqueConstraint("scope", name="uq_rate_limit_scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str]  # "global" or a contact namespace
    window_start: Mapped[datetime]
    count: Mapped[int] = mapped_column(default=0)


class UsageMeter(Base):
    """Per-cycle usage in one native currency + its budget mode (#84, part of #72).

    Replaces the single Anthropic-dollar ``monthly_costs`` accumulator: chief now meters
    each turn in the native currency it actually spent — Copilot **premium requests**
    (a raw count vs the 200/mo cap), **OpenRouter dollars** (metered spend vs a dollar
    cap), or **bridge turns** (an informational count, no cap). No cross-conversion.

    One row per ``(cycle, currency)`` — the cycle key (``"2026-06"``-style) is anchored
    in ``owner_tz``, so a new cycle has no row and the currency resets to 0 implicitly.
    ``amount`` is a read-modify-write accumulator (additive for dollars/bridge, or
    monotonic for the premium snapshot), so the unique constraint stops a racing insert
    duplicating a row; the repo also serializes get-or-create under a lock. ``mode`` is
    the per-currency budget state a threshold action or the owner's card decision flips
    (``usage.MODE_*``); ``warned_fraction`` is that currency's last-warned high-water
    mark, so each tier warns once.
    """

    __tablename__ = "usage_meters"
    __table_args__ = (
        UniqueConstraint("cycle", "currency", name="uq_usage_meter_cycle_currency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle: Mapped[str]  # billing-cycle key, e.g. "2026-06"
    currency: Mapped[str]  # usage.PREMIUM_REQUESTS / OPENROUTER_DOLLARS / BRIDGE_TURNS
    amount: Mapped[float] = mapped_column(default=0.0)  # count or dollars per currency
    mode: Mapped[str] = mapped_column(default="normal")  # usage.MODE_* (string const)
    warned_fraction: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class Schedule(Base):
    """A one-off reminder, a recurring job, or a monitor (owned by M9).

    A ``monitor`` row reuses ``spec`` as its check **cadence** (a cron expression) and
    adds a predicate: each cadence tick checks ``predicate`` (``predicate_type`` picks
    bash-vs-agent) and fires only on a false→true flip (``last_result`` is the last
    evaluated truth, ``None`` until first checked). The predicate columns are NULL for
    ``once``/``recurring`` rows.
    """

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]  # "once" | "recurring" | "monitor" — picks next_run advance logic
    spec: Mapped[str]  # ISO ts ("once"), or a cron expression ("recurring"/"monitor")
    action: Mapped[str | None]  # reminder text, wakeup prompt, or bash command
    action_type: Mapped[str]  # "message" | "wakeup" | "bash" — the fire path
    thread_key: Mapped[str | None]  # delivery/wake target; None ⇒ primary inbox
    urgent: Mapped[bool] = mapped_column(default=False)  # True breaks quiet hours
    next_run: Mapped[datetime | None]
    last_run: Mapped[datetime | None]  # last fire (catch-up + /schedules listing)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    # Monitor-only (NULL for once/recurring): the watched condition + flip-detect state.
    predicate: Mapped[str | None] = mapped_column(default=None)  # bash cmd / agent ask
    predicate_type: Mapped[str | None] = mapped_column(default=None)  # "bash" | "agent"
    last_result: Mapped[bool | None] = mapped_column(default=None)  # None ⇒ never run
