"""Usage-budget coordinator (M9) — record spend, warn, and gate on exhaustion.

After the June-15 billing change chief draws from a fixed monthly credit, so the risk
is silently blowing it early. :class:`BudgetGate` rolls each turn's SDK cost into the
billing cycle's month-to-date total (:mod:`chief.persistence.usage`), warns the owner
once per configured threshold, and on (near-)exhaustion flips the cycle into the
``paused`` mode and posts a **choice card** to the owner inbox (downgrade / continue /
overflow). The turn that trips exhaustion has already spent; *subsequent* turns are what
the persisted mode gates — restart-proof, no parked future (the admission-card pattern).

Mostly pure + a thin IO seam, so it is testable without the SDK: :func:`cycle_key` is
pure, and everything else flows through ``session_factory`` + a small :class:`BudgetIO`.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..persistence import usage

logger = logging.getLogger("chief.core.budget")


class BudgetIO(Protocol):
    """The slice of the platform IO the budget gate delivers to (owner inbox only)."""

    async def send(self, thread_key: str, text: str) -> None: ...
    async def send_budget_card(
        self, thread_key: str, *, cycle: str, text: str
    ) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def cycle_key(now: datetime, tz: ZoneInfo, anchor_day: int) -> str:
    """The billing cycle ``now`` falls in, as a ``"YYYY-MM"`` key (cycle-start month).

    Anchored in the owner's ``tz``: the cycle resets on ``anchor_day`` each month, so a
    local date *before* ``anchor_day`` belongs to the cycle that started the prior month
    (``anchor_day == 1`` collapses to the plain calendar month). The key names the
    cycle's start year-month, so a new cycle simply has no row → spend resets to 0.
    """
    local = now.astimezone(tz)
    year, month = local.year, local.month
    if local.day < anchor_day:
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return f"{year:04d}-{month:02d}"


class BudgetGate:
    """Records per-turn spend; warns and pauses the cycle as the credit runs down."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        io: BudgetIO,
        owner_inbox: str,
        monthly_credit_usd: float,
        warn_fractions: tuple[float, ...] = (0.75, 0.90),
        exhaust_fraction: float = 1.0,
        owner_tz: str = "UTC",
        anchor_day: int = 1,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._io = io
        self._owner_inbox = owner_inbox
        self._credit = monthly_credit_usd
        self._warn_fractions = tuple(sorted(warn_fractions))
        self._exhaust_fraction = exhaust_fraction
        self._tz = ZoneInfo(owner_tz)
        self._anchor_day = anchor_day
        self._now = now

    def _cycle(self) -> str:
        return cycle_key(self._now(), self._tz, self._anchor_day)

    async def record(self, cost: float) -> None:
        """Roll ``cost`` into the cycle total, then warn or pause+card as it crosses."""
        cycle = self._cycle()
        async with self._session_factory() as session:
            total = await usage.add_cost(session, cycle=cycle, amount=cost)
            row = await usage.get_row(session, cycle)
            assert row is not None  # add_cost just created/updated it
            fraction = total / self._credit
            if fraction >= self._exhaust_fraction:
                await self._exhaust(session, cycle, row.mode, total)
            else:
                await self._warn(session, cycle, row.warned_fraction, total, fraction)

    async def note_rate_limited(self) -> None:
        """A hard ``rejected`` rate limit — treat like exhaustion: pause + ask."""
        cycle = self._cycle()
        async with self._session_factory() as session:
            row = await usage.get_row(session, cycle)
            mode = row.mode if row is not None else usage.MODE_NORMAL
            total = row.total_cost_usd if row is not None else 0.0
            await self._exhaust(session, cycle, mode, total, rate_limited=True)

    async def mode(self) -> str:
        """The cycle's persisted budget mode (``MODE_NORMAL`` when no row yet)."""
        async with self._session_factory() as session:
            row = await usage.get_row(session, self._cycle())
            return row.mode if row is not None else usage.MODE_NORMAL

    async def _warn(
        self,
        session: AsyncSession,
        cycle: str,
        warned_fraction: float,
        total: float,
        fraction: float,
    ) -> None:
        """Warn once for the highest tier newly crossed past the high-water mark."""
        crossed = [f for f in self._warn_fractions if warned_fraction < f <= fraction]
        if not crossed:
            return
        await usage.mark_warned(session, cycle=cycle, fraction=crossed[-1])
        await self._io.send(self._owner_inbox, self._warn_text(total, fraction))

    async def _exhaust(
        self,
        session: AsyncSession,
        cycle: str,
        mode: str,
        total: float,
        *,
        rate_limited: bool = False,
    ) -> None:
        """Flip a still-``normal`` cycle to ``paused`` and post the choice card once.

        Only ``normal`` is acted on: once the owner has chosen (continue/overflow/
        downgraded) or it is already paused, further over-budget turns must not re-pause
        or re-card.
        """
        if mode != usage.MODE_NORMAL:
            return
        await usage.set_mode(session, cycle=cycle, mode=usage.MODE_PAUSED)
        # Mark every tier as warned so no stale threshold warning fires after the card.
        await usage.mark_warned(session, cycle=cycle, fraction=1.0)
        await self._io.send_budget_card(
            self._owner_inbox, cycle=cycle, text=self._card_text(total, rate_limited)
        )

    def _warn_text(self, total: float, fraction: float) -> str:
        return (
            f"⚠️ Budget: ${total:.2f} of ${self._credit:.2f} used "
            f"({fraction:.0%} of the monthly credit)."
        )

    def _card_text(self, total: float, rate_limited: bool) -> str:
        if rate_limited:
            head = "🛑 Rate limited by the API — pausing to avoid burning credit."
        else:
            head = f"🛑 Budget reached: ${total:.2f} of ${self._credit:.2f} used."
        return f"{head} Pick how to continue:"
