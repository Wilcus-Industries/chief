"""Rate-limit repository — a fixed-window counter per ``scope`` (M6).

Guards the owner's Max limits against guest abuse/cost: each guest message counts
against both a per-guest scope (the contact namespace) and a ``"global"`` guest budget.
The window is fixed (not sliding) — simple, restart-safe (persisted in ``rate_limits``),
and good enough for spam protection. The caller supplies the window size + cap; ``now``
is injected so the window logic is testable without sleeping.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import RateLimit

#: Serializes the read-modify-write across the one event loop, so two concurrent guest
#: messages can't both miss the row and double-insert (the ``"global"`` scope is shared
#: by every guest). The DB unique constraint on ``scope`` is the belt-and-braces guard.
_lock = asyncio.Lock()


async def check_and_increment(
    session: AsyncSession,
    *,
    scope: str,
    window_seconds: int,
    limit: int,
    now: datetime | None = None,
) -> bool:
    """Return ``True`` (and count it) if ``scope`` is within ``limit`` this window.

    A missing or expired window resets to a fresh count of 1. Once at the cap the call
    is rejected and not counted (the stored count stays a clean "calls in this window").
    """
    async with _lock:
        return await _check_and_increment(
            session, scope=scope, window_seconds=window_seconds, limit=limit, now=now
        )


async def _check_and_increment(
    session: AsyncSession,
    *,
    scope: str,
    window_seconds: int,
    limit: int,
    now: datetime | None,
) -> bool:
    moment = now or datetime.now(UTC)
    row = (
        await session.execute(select(RateLimit).where(RateLimit.scope == scope))
    ).scalar_one_or_none()

    if row is None:
        session.add(RateLimit(scope=scope, window_start=moment, count=1))
        await session.commit()
        return True

    window_start = row.window_start
    if window_start.tzinfo is None:  # sqlite round-trips naive datetimes
        window_start = window_start.replace(tzinfo=UTC)
    if moment - window_start >= timedelta(seconds=window_seconds):
        row.window_start = moment
        row.count = 1
        await session.commit()
        return True

    if row.count >= limit:
        return False

    row.count += 1
    await session.commit()
    return True
