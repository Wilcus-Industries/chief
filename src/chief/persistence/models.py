"""SQLAlchemy schema. Fresh v2 schema — created with ``create_all``, no Alembic."""

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all chief tables."""


class _Stamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class SessionRow(_Stamped, Base):
    """One conversation thread and its per-thread settings."""

    __tablename__ = "sessions"

    thread_key: Mapped[str] = mapped_column(String, primary_key=True)
    channel: Mapped[str] = mapped_column(String)
    model_override: Mapped[str | None] = mapped_column(String, default=None)
    # Per-thread stream policy (StreamPolicy.to_dict); None = fall back to the
    # channel default. Nullable so _reconcile_columns adds it to legacy DBs.
    stream_policy: Mapped[dict[str, object] | None] = mapped_column(
        JSON, default=None
    )


class MessageRow(_Stamped, Base):
    """One transcript entry, stored as the provider-seam wire dict."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_key: Mapped[str] = mapped_column(String, index=True)
    message: Mapped[dict[str, object]] = mapped_column(JSON)


class MonitorRow(_Stamped, Base):
    """An agent-created event subscription that wakes a thread when it fires."""

    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    description: Mapped[str] = mapped_column(String)
    watch_channel: Mapped[str] = mapped_column(String)
    wake_channel: Mapped[str] = mapped_column(String)
    wake_thread: Mapped[str] = mapped_column(String)
    predicate: Mapped[dict[str, object]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ScheduleRow(_Stamped, Base):
    """A schedule that wakes a thread with a prompt, or runs a shell command."""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    description: Mapped[str] = mapped_column(String)
    spec: Mapped[str] = mapped_column(String)
    wake_channel: Mapped[str] = mapped_column(String)
    wake_thread: Mapped[str] = mapped_column(String)
    prompt: Mapped[str] = mapped_column(String, default="")
    # Exactly one of prompt/command is set: a command row fires unattended on
    # its own shell and never wakes the agent, so it carries no prompt.
    command: Mapped[str | None] = mapped_column(String, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class SpendRow(_Stamped, Base):
    """One turn's dollar cost, for the per-cycle budget."""

    __tablename__ = "spend"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_key: Mapped[str] = mapped_column(String)
    cost: Mapped[float] = mapped_column(Float)


class StrangerRow(_Stamped, Base):
    """Metadata-only record of a message from an unknown sender (no content)."""

    __tablename__ = "strangers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String)
    sender: Mapped[str] = mapped_column(String)
    thread_key: Mapped[str] = mapped_column(String)
