"""Watches repository (#165/#168, part of PRD #160) — standing owner instructions.

One row per watch, created/listed/cancelled here; nothing fires yet (that's a later
milestone). Mirrors :mod:`chief.persistence.schedules`: module-level async fns over
an ``AsyncSession``, with the watch vocabulary (``tone`` / ``state``) as constants so
the engine and the owner's watch tools share one source of truth. Reuses
:func:`chief.persistence.imessage.normalize_handle` for handle normalization — no
second parser.

#168 adds the unknown-sender confirm flow: a watch created with no
``target_handle`` starts unbound, each new unknown sender surfaces as a
``WatchCandidate`` (metadata only — handle + timestamp, never content), and
confirming one binds the watch — an ordinary armed ``Watch`` row indistinguishable
from a directly-created one, ready for a later milestone's dispatch with zero
special-casing.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .imessage import normalize_handle
from .models import Watch, WatchCandidate

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

#: Candidate decision states (#168, part of PRD #160).
CANDIDATE_PENDING = "pending"
CANDIDATE_CONFIRMED = "confirmed"
CANDIDATE_REJECTED = "rejected"


def default_expiry(now: datetime) -> datetime:
    """The 14-day default TTL from ``now`` (UTC-aware in and out)."""
    return now + timedelta(days=DEFAULT_TTL_DAYS)


async def create_watch(
    session: AsyncSession,
    *,
    target_handle: str | None,
    instruction: str,
    expiry: datetime,
    tone: str = TONE_REPORT,
) -> Watch:
    """Insert an armed watch and return it.

    ``target_handle=None`` creates an unbound watch (#168): the owner didn't
    resolve a handle yet, so it starts awaiting a confirmed candidate instead.
    """
    watch = Watch(
        target_handle=normalize_handle(target_handle) if target_handle else None,
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


# ---- unbound-watch candidates (#168, part of PRD #160) ---------------------------


async def list_unbound_watches(session: AsyncSession, *, now: datetime) -> list[Watch]:
    """Every armed, unexpired watch with no target handle yet (#168) — the pool an
    unknown-sender row is checked against."""
    stmt = select(Watch).where(
        Watch.target_handle.is_(None), Watch.state == STATE_ARMED
    )
    rows = list((await session.execute(stmt)).scalars())
    return [w for w in rows if effective_state(w, now=now) == STATE_ARMED]


async def get_candidate(
    session: AsyncSession, watch_id: int, handle: str
) -> WatchCandidate | None:
    """The candidate row for this (watch, handle) pair, if one already exists."""
    stmt = select(WatchCandidate).where(
        WatchCandidate.watch_id == watch_id,
        WatchCandidate.handle == normalize_handle(handle),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_candidate(
    session: AsyncSession, *, watch_id: int, handle: str, first_seen: datetime
) -> WatchCandidate:
    """Insert a pending candidate sighting and return it."""
    candidate = WatchCandidate(
        watch_id=watch_id, handle=normalize_handle(handle), first_seen=first_seen
    )
    session.add(candidate)
    await session.commit()
    await session.refresh(candidate)
    return candidate


async def list_pending_candidates(
    session: AsyncSession, *, now: datetime
) -> list[WatchCandidate]:
    """Pending candidates whose watch is still armed (#168) — an expired or
    cancelled watch's pending candidates stop surfacing and stop being confirmable."""
    stmt = (
        select(WatchCandidate, Watch)
        .join(Watch, WatchCandidate.watch_id == Watch.id)
        .where(WatchCandidate.decision == CANDIDATE_PENDING)
        .order_by(WatchCandidate.first_seen.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [c for c, w in rows if effective_state(w, now=now) == STATE_ARMED]


async def confirm_candidate(
    session: AsyncSession, candidate_id: int, *, confirm: bool, now: datetime
) -> tuple[Watch, WatchCandidate] | None:
    """Resolve a pending candidate. ``None`` if missing, already decided, or its
    watch is no longer armed (expiry/cancel kills pending candidates — #168 AC)."""
    candidate = await session.get(WatchCandidate, candidate_id)
    if candidate is None or candidate.decision != CANDIDATE_PENDING:
        return None
    watch = await session.get(Watch, candidate.watch_id)
    if watch is None or effective_state(watch, now=now) != STATE_ARMED:
        return None
    candidate.decision = CANDIDATE_CONFIRMED if confirm else CANDIDATE_REJECTED
    candidate.decided_at = now
    if confirm:
        watch.target_handle = candidate.handle
        watch.confirmed_at = now
    await session.commit()
    await session.refresh(watch)
    await session.refresh(candidate)
    return watch, candidate
