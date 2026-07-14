"""Watches repository (#165, part of PRD #160) — standing owner instructions.

One row per watch, created/listed/cancelled here; nothing fires yet (that's a later
milestone). Mirrors :mod:`chief.persistence.schedules`: module-level async fns over
an ``AsyncSession``, with the watch vocabulary (``tone`` / ``state``) as constants so
the engine and the owner's watch tools share one source of truth. Reuses
:func:`chief.persistence.imessage.normalize_handle` for handle normalization — no
second parser.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .imessage import normalize_handle
from .models import Watch

#: Default TTL for a watch with no explicit expiry.
DEFAULT_TTL_DAYS = 14

#: Reporting tones — how a firing watch (a later milestone) notifies the owner.
TONE_REPORT = "report"  # tell the owner what happened
TONE_SILENT = "silent"  # act, but don't narrate back
TONES = (TONE_REPORT, TONE_SILENT)

#: Lifecycle states. ``fired``/``expired`` are reserved for the firing milestone;
#: this slice only ever writes ``armed`` (on create) and ``cancelled`` (on cancel).
STATE_ARMED = "armed"
STATE_FIRED = "fired"
STATE_EXPIRED = "expired"
STATE_CANCELLED = "cancelled"


def default_expiry(now: datetime) -> datetime:
    """The 14-day default TTL from ``now`` (UTC-aware in and out)."""
    return now + timedelta(days=DEFAULT_TTL_DAYS)


async def create_watch(
    session: AsyncSession,
    *,
    target_handle: str,
    instruction: str,
    expiry: datetime,
    tone: str = TONE_REPORT,
) -> Watch:
    """Insert an armed watch and return it."""
    watch = Watch(
        target_handle=normalize_handle(target_handle),
        instruction=instruction,
        expiry=expiry,
        tone=tone,
    )
    session.add(watch)
    await session.commit()
    await session.refresh(watch)
    return watch


async def get_watch(session: AsyncSession, watch_id: int) -> Watch | None:
    """Return the watch with ``watch_id`` or ``None``."""
    return await session.get(Watch, watch_id)


async def list_watches(session: AsyncSession) -> list[Watch]:
    """Every watch, newest first — for the ``/watches`` listing and the web panel."""
    stmt = select(Watch).order_by(Watch.created_at.desc())
    return list((await session.execute(stmt)).scalars())


async def cancel_watch(session: AsyncSession, watch_id: int) -> Watch | None:
    """Cancel an armed watch; ``None`` if missing or not armed (idempotent no-op —
    callers check :func:`effective_state` first for a clear message)."""
    watch = await get_watch(session, watch_id)
    if watch is None or watch.state != STATE_ARMED:
        return None
    watch.state = STATE_CANCELLED
    await session.commit()
    return watch


def effective_state(watch: Watch, *, now: datetime) -> str:
    """The watch's DISPLAY state: ``armed`` reads as ``expired`` once past ``expiry``.

    Read-only — nothing here mutates the row. The real armed→expired transition
    (and any fire) belongs to the firing milestone (PRD #160); this keeps listings
    truthful today without that sweep existing yet.
    """
    expiry = watch.expiry
    stamped = expiry if expiry.tzinfo is not None else expiry.replace(tzinfo=UTC)
    if watch.state == STATE_ARMED and stamped <= now:
        return STATE_EXPIRED
    return watch.state
