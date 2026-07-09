"""Per-currency usage repository — native-currency accounting + budget mode (#84).

chief meters each turn in the native currency it actually spent, replacing the old
Anthropic-dollar month-to-date accumulator (part of #72):

* :data:`PREMIUM_REQUESTS` — Copilot premium-request count against the monthly cap. The
  Copilot SDK reports a **cumulative** used-request snapshot per cycle, so this currency
  accumulates by :func:`raise_amount` (monotonic max) — a repeated or stale reading
  never double-counts a turn.
* :data:`OPENROUTER_DOLLARS` — metered OpenRouter spend against a dollar cap; a turn's
  cost is additive (:func:`add_amount`).
* :data:`BRIDGE_TURNS` — an informational turn count on the Max bridge (no cap/action).

One row per ``(cycle, currency)`` — the cycle key is anchored in ``owner_tz``, so a new
cycle has no row and the currency resets implicitly. ``mode`` carries the per-currency
budget state a threshold action or the owner's choice-card flips. Mirrors
``rate_limits.py``: module-level async fns over an ``AsyncSession``, read-modify-write
serialized by a lock (the DB unique constraint is the belt-and-braces guard).
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import UsageMeter

#: The three native currencies chief budgets in — no cross-currency conversion (#84).
PREMIUM_REQUESTS = "premium_requests"  # Copilot premium requests vs the monthly cap
OPENROUTER_DOLLARS = "openrouter_dollars"  # metered OpenRouter spend vs a dollar cap
BRIDGE_TURNS = "bridge_turns"  # informational Max-bridge turn count (no cap)

#: Persisted per-currency budget modes for ``UsageMeter.mode`` (a threshold action or
#: the owner's card choice picks one).
MODE_NORMAL = "normal"  # under budget, turns run full-quality
MODE_PAUSED = "paused"  # exhausted: owner turns gated until a card choice
MODE_DOWNGRADED = "downgraded"  # re-targeted to the cheaper class for this cycle
MODE_CONTINUE = "continue"  # owner chose to keep going despite exhaustion
MODE_OVERFLOW = "overflow"  # owner approved spend past the cap

#: Serializes the read-modify-write across the one event loop, so two turns recording in
#: a fresh cycle can't both miss the row and double-insert. The DB unique constraint on
#: ``(cycle, currency)`` is the belt-and-braces guard.
_lock = asyncio.Lock()


async def get_row(
    session: AsyncSession, cycle: str, currency: str
) -> UsageMeter | None:
    """Return the meter row for ``(cycle, currency)`` or ``None`` (pure read)."""
    stmt = select(UsageMeter).where(
        UsageMeter.cycle == cycle, UsageMeter.currency == currency
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_amount(
    session: AsyncSession, *, cycle: str, currency: str, amount: float
) -> float:
    """Add ``amount`` to ``(cycle, currency)``'s month-to-date total; return the total.

    For a currency whose meter reports a **per-turn delta** (OpenRouter dollars, bridge
    turns): the deltas sum into the running total.
    """
    async with _lock:
        row = await _get_or_create(session, cycle, currency)
        row.amount += amount
        await session.commit()
        return row.amount


async def raise_amount(
    session: AsyncSession, *, cycle: str, currency: str, amount: float
) -> float:
    """Raise ``(cycle, currency)``'s total to ``amount`` (monotonic max); return it.

    For a currency whose meter reports a **cumulative** snapshot (premium requests): a
    lower or repeated reading can't lower the total, so a turn is never double-counted.
    """
    async with _lock:
        row = await _get_or_create(session, cycle, currency)
        row.amount = max(row.amount, amount)
        await session.commit()
        return row.amount


async def set_mode(
    session: AsyncSession, *, cycle: str, currency: str, mode: str
) -> None:
    """Flip ``(cycle, currency)``'s persisted budget mode (one of the ``MODE_*``)."""
    async with _lock:
        row = await _get_or_create(session, cycle, currency)
        row.mode = mode
        await session.commit()


async def mark_warned(
    session: AsyncSession, *, cycle: str, currency: str, fraction: float
) -> None:
    """Raise ``(cycle, currency)``'s warned mark to ``fraction`` (never lowers it).

    Monotonic so the "each tier warns once" invariant holds regardless of call order: a
    lower fraction (an out-of-order or recomputed warn) can't re-arm a warned tier.
    """
    async with _lock:
        row = await _get_or_create(session, cycle, currency)
        row.warned_fraction = max(row.warned_fraction, fraction)
        await session.commit()


async def _get_or_create(
    session: AsyncSession, cycle: str, currency: str
) -> UsageMeter:
    """Return ``(cycle, currency)``'s row, inserting a fresh one if absent (locked)."""
    row = await get_row(session, cycle, currency)
    if row is None:
        row = UsageMeter(cycle=cycle, currency=currency)
        session.add(row)
        await session.flush()
    return row
