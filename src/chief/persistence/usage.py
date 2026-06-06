"""Monthly-cost repository — month-to-date SDK spend + budget mode (M9).

After the June-15 billing change chief draws from a fixed monthly credit, so the risk
is silently blowing it early. Each turn's ``total_cost_usd`` rolls into one row per
billing cycle (the cycle key is anchored in ``owner_tz``; a new cycle has no row, so
spend resets implicitly). The ``mode`` column carries the budget state the owner's
choice-card decision flips, persisted so it survives a restart (the admission-card
pattern, no parked future).

Mirrors ``rate_limits.py``: module-level async fns over an ``AsyncSession``,
read-modify-write, ``UniqueConstraint`` + a lock to serialize the get-or-create.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import MonthlyCost

#: Persisted budget modes for ``MonthlyCost.mode`` (the owner's card choice picks one).
MODE_NORMAL = "normal"  # under budget, turns run full-quality
MODE_PAUSED = "paused"  # exhausted: owner turns gated until a card choice
MODE_DOWNGRADED = "downgraded"  # owner chose to keep running on the cheaper model
MODE_CONTINUE = "continue"  # owner chose to keep full-quality despite exhaustion
MODE_OVERFLOW = "overflow"  # owner approved pay-as-you-go spend past the credit

#: Serializes the read-modify-write across the one event loop, so two turns recording
#: cost in a fresh cycle can't both miss the row and double-insert. The DB unique
#: constraint on ``cycle`` is the belt-and-braces guard.
_lock = asyncio.Lock()


async def get_row(session: AsyncSession, cycle: str) -> MonthlyCost | None:
    """Return the spend row for ``cycle`` or ``None`` (pure read)."""
    stmt = select(MonthlyCost).where(MonthlyCost.cycle == cycle)
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_cost(session: AsyncSession, *, cycle: str, amount: float) -> float:
    """Add ``amount`` to ``cycle``'s month-to-date spend; return the new total."""
    async with _lock:
        row = await _get_or_create(session, cycle)
        row.total_cost_usd += amount
        await session.commit()
        return row.total_cost_usd


async def set_mode(session: AsyncSession, *, cycle: str, mode: str) -> None:
    """Flip ``cycle``'s persisted budget mode (one of the ``MODE_*`` constants)."""
    async with _lock:
        row = await _get_or_create(session, cycle)
        row.mode = mode
        await session.commit()


async def mark_warned(session: AsyncSession, *, cycle: str, fraction: float) -> None:
    """Raise ``cycle``'s warned high-water mark to ``fraction`` (never lowers it).

    Monotonic so the "each tier warns once" invariant holds regardless of call order:
    a lower fraction (an out-of-order or recomputed warn) can't re-arm an already-warned
    tier.
    """
    async with _lock:
        row = await _get_or_create(session, cycle)
        row.warned_fraction = max(row.warned_fraction, fraction)
        await session.commit()


async def _get_or_create(session: AsyncSession, cycle: str) -> MonthlyCost:
    """Return ``cycle``'s row, inserting a fresh one if absent (under ``_lock``)."""
    row = await get_row(session, cycle)
    if row is None:
        row = MonthlyCost(cycle=cycle)
        session.add(row)
        await session.flush()
    return row
