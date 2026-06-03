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


class RateLimit(Base):
    """Per-guest and global rate-limit counters (owned by M6)."""

    __tablename__ = "rate_limits"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str]  # "global" or a contact namespace
    window_start: Mapped[datetime]
    count: Mapped[int] = mapped_column(default=0)


class Schedule(Base):
    """A reminder, recurring job, or monitor (owned by M9)."""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]  # "reminder" | "recurring" | "monitor"
    spec: Mapped[str]
    action: Mapped[str | None]
    next_run: Mapped[datetime | None]
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
