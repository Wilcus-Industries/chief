"""Per-currency usage-budget coordinator (#84) — meter, warn, pause/downgrade.

chief's spend rides two native currencies with no cross-currency conversion (part of
#72): Copilot **premium requests** (a raw count vs the 200/mo cap) and **OpenRouter
dollars** (metered spend vs a dollar cap).
:class:`BudgetGate` rolls each turn's usage into its currency's month-to-date row
(:mod:`chief.persistence.usage`), warns the owner once per configured threshold, and on
(near-)exhaustion runs that currency's configured action:

* :data:`ACTION_PAUSE` (premium requests) — flip the currency to ``paused`` and post the
  owner a choice card; the paused mode gates *subsequent* turns (the admission-card
  pattern, restart-proof — the turn that tripped exhaustion has already spent).
* :data:`ACTION_DOWNGRADE` (OpenRouter dollars) — flip the currency to ``downgraded``
  and return :data:`EFFECT_DOWNGRADE`, so the engine re-targets the openrouter routes
  onto the cheaper Copilot ``auto`` class (an overlay the resolver reads — no routing
  rows mutated) and switches live openrouter sessions across.

A currency configured with a non-positive cap meters but never warns or acts.

Mostly pure + a thin IO seam, so it is testable without an SDK: :func:`cycle_key` is
pure and everything else flows through ``session_factory`` + a small :class:`BudgetIO`.
The engine, not the gate, owns the live sessions, so an exhaustion needing a live switch
is *returned* as an effect rather than called back — keeping the wiring one-directional
(engine → gate) with no back-reference.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.base import BudgetCard
from ..persistence import usage

logger = logging.getLogger("chief.core.budget")

#: How a currency folds each reading into its month-to-date total.
ACCUM_ADD = "add"  # per-turn deltas sum (OpenRouter dollars)
ACCUM_MAX = "max"  # a cumulative snapshot; keep the high-water mark (premium requests)

#: What a currency does when it crosses its exhaustion threshold.
ACTION_PAUSE = "pause"  # flip to paused + post the owner choice card
ACTION_DOWNGRADE = "downgrade"  # flip to downgraded + tell the engine to re-target

#: Returned by :meth:`BudgetGate.record` / :meth:`BudgetGate.note_rate_limited` when a
#: currency just crossed into ``downgraded`` — the engine completes it by switching live
#: sessions onto the cheaper class (:meth:`TaskManager.downgrade_live_sessions`).
EFFECT_DOWNGRADE = "downgrade"

#: Owner-facing currency labels + amount formatters for warn/card/downgrade text.
_CURRENCY_LABEL = {
    usage.PREMIUM_REQUESTS: "premium requests",
    usage.OPENROUTER_DOLLARS: "OpenRouter spend",
}


def _fmt_amount(currency: str, amount: float) -> str:
    """Format ``amount`` in ``currency``'s natural unit (dollars vs a plain count)."""
    if currency == usage.OPENROUTER_DOLLARS:
        return f"${amount:.2f}"
    return f"{amount:.0f}"


def premium_request_total(counts: dict[str, int]) -> float:
    """Sum a turn's raw per-quota premium-request snapshot into one cumulative count.

    ``counts`` is :attr:`CopilotTaskSession.last_premium_requests` (#80) — quota name →
    cumulative used-requests this cycle. The Student plan exposes a single premium pool,
    so the sum is that pool's running count; an empty dict (no snapshot) is ``0.0``.
    """
    return float(sum(counts.values()))


@dataclass(frozen=True)
class CurrencyPolicy:
    """One native currency's cap, warn/exhaust thresholds, accumulation, and action."""

    cap: float
    warn_fractions: tuple[float, ...]
    exhaust_fraction: float
    accumulation: str  # ACCUM_ADD | ACCUM_MAX
    action: str  # ACTION_PAUSE | ACTION_DOWNGRADE


@dataclass(frozen=True)
class CurrencySnapshot:
    """One currency's cycle spend for the ``/status`` view (#138)."""

    currency: str
    spent: float
    cap: float
    mode: str


class BudgetIO(Protocol):
    """The slice of the platform IO the budget gate delivers to (owner inbox only)."""

    async def send(self, thread_key: str, text: str) -> None: ...
    async def send_budget_card(self, route: str, card: BudgetCard) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def cycle_key(now: datetime, tz: ZoneInfo, anchor_day: int) -> str:
    """The billing cycle ``now`` falls in, as a ``"YYYY-MM"`` key (cycle-start month).

    Anchored in the owner's ``tz``: the cycle resets on ``anchor_day`` each month, so a
    local date *before* ``anchor_day`` belongs to the cycle that started the prior month
    (``anchor_day == 1`` collapses to the plain calendar month). The key names the
    cycle's start year-month, so a new cycle simply has no row → usage resets to 0.
    """
    local = now.astimezone(tz)
    year, month = local.year, local.month
    if local.day < anchor_day:
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return f"{year:04d}-{month:02d}"


class BudgetGate:
    """Meters each turn per currency; warns and runs its exhaustion action (#84)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        io: BudgetIO,
        owner_inbox: str,
        policies: dict[str, CurrencyPolicy],
        owner_tz: str = "UTC",
        anchor_day: int = 1,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._io = io
        self._owner_inbox = owner_inbox
        self._policies = policies
        self._tz = ZoneInfo(owner_tz)
        self._anchor_day = anchor_day
        self._now = now
        #: Serializes the whole read-decide-write of record/note_rate_limited across the
        #: one event loop. The usage repo's per-op lock only guards each get-or-create;
        #: the warn-once / act-once decision spans several ops, so without this two
        #: concurrent turns crossing a threshold could both read mode==normal and both
        #: act. (Distinct from usage._lock — acquired before it, never re-entered.)
        self._lock = asyncio.Lock()

    def _cycle(self) -> str:
        return cycle_key(self._now(), self._tz, self._anchor_day)

    async def record(self, currency: str, amount: float) -> str | None:
        """Roll ``amount`` into ``currency``'s cycle total, then warn or run its action.

        Returns :data:`EFFECT_DOWNGRADE` when the currency just crossed into
        ``downgraded`` (the engine completes it), else ``None``. Unknown currency: skip.
        """
        policy = self._policies.get(currency)
        if policy is None:
            return None
        async with self._lock:
            cycle = self._cycle()
            async with self._session_factory() as session:
                total = await self._accumulate(session, cycle, currency, policy, amount)
                if policy.cap <= 0:
                    return None  # uncapped currency — metered, never warns or acts
                row = await usage.get_row(session, cycle, currency)
                assert row is not None  # _accumulate just created/updated it
                fraction = total / policy.cap
                if fraction >= policy.exhaust_fraction:
                    return await self._exhaust(
                        session, cycle, currency, policy, row.mode, total
                    )
                await self._warn(
                    session,
                    cycle,
                    currency,
                    policy,
                    row.warned_fraction,
                    total,
                    fraction,
                )
                return None

    async def note_rate_limited(self, currency: str) -> str | None:
        """A hard ``rejected`` rate limit on ``currency`` — treat like exhaustion.

        Runs the currency's exhaustion action (pause+card, or downgrade); returns
        :data:`EFFECT_DOWNGRADE` when it downgraded, else ``None``. An unknown currency
        is a no-op.
        """
        policy = self._policies.get(currency)
        if policy is None:
            return None
        async with self._lock:
            cycle = self._cycle()
            async with self._session_factory() as session:
                row = await usage.get_row(session, cycle, currency)
                mode = row.mode if row is not None else usage.MODE_NORMAL
                total = row.amount if row is not None else 0.0
                return await self._exhaust(
                    session, cycle, currency, policy, mode, total, rate_limited=True
                )

    async def mode(self, currency: str) -> str:
        """``currency``'s persisted mode this cycle (``MODE_NORMAL`` when no row)."""
        async with self._session_factory() as session:
            row = await usage.get_row(session, self._cycle(), currency)
            return row.mode if row is not None else usage.MODE_NORMAL

    async def snapshot(self) -> list[CurrencySnapshot]:
        """Every configured currency's cycle spend, cap, and mode (#138 ``/status``).

        Read-only — never inserts a row; an unspent currency reports ``spent=0.0`` and
        ``mode=MODE_NORMAL`` straight from the policy.
        """
        cycle = self._cycle()
        out: list[CurrencySnapshot] = []
        async with self._session_factory() as session:
            for currency, policy in self._policies.items():
                row = await usage.get_row(session, cycle, currency)
                spent = row.amount if row is not None else 0.0
                mode = row.mode if row is not None else usage.MODE_NORMAL
                out.append(
                    CurrencySnapshot(
                        currency=currency, spent=spent, cap=policy.cap, mode=mode
                    )
                )
        return out

    async def _accumulate(
        self,
        session: AsyncSession,
        cycle: str,
        currency: str,
        policy: CurrencyPolicy,
        amount: float,
    ) -> float:
        if policy.accumulation == ACCUM_MAX:
            return await usage.raise_amount(
                session, cycle=cycle, currency=currency, amount=amount
            )
        return await usage.add_amount(
            session, cycle=cycle, currency=currency, amount=amount
        )

    async def _warn(
        self,
        session: AsyncSession,
        cycle: str,
        currency: str,
        policy: CurrencyPolicy,
        warned_fraction: float,
        total: float,
        fraction: float,
    ) -> None:
        """Warn once for the highest tier newly crossed past the high-water mark."""
        tiers = sorted(policy.warn_fractions)
        crossed = [f for f in tiers if warned_fraction < f <= fraction]
        if not crossed:
            return
        await usage.mark_warned(
            session, cycle=cycle, currency=currency, fraction=crossed[-1]
        )
        await self._io.send(
            self._owner_inbox, self._warn_text(currency, policy, total, fraction)
        )

    async def _exhaust(
        self,
        session: AsyncSession,
        cycle: str,
        currency: str,
        policy: CurrencyPolicy,
        mode: str,
        total: float,
        *,
        rate_limited: bool = False,
    ) -> str | None:
        """Run ``currency``'s exhaustion action once, only while it is still ``normal``.

        Once a threshold action has fired (or the owner has chosen), further over-budget
        turns in the same cycle must not re-pause, re-card, or re-downgrade.
        """
        if mode != usage.MODE_NORMAL:
            return None
        # Mark every tier warned so no stale threshold warning fires after the action.
        await usage.mark_warned(session, cycle=cycle, currency=currency, fraction=1.0)
        if policy.action == ACTION_DOWNGRADE:
            await usage.set_mode(
                session, cycle=cycle, currency=currency, mode=usage.MODE_DOWNGRADED
            )
            await self._io.send(
                self._owner_inbox, self._downgrade_text(currency, policy, total)
            )
            return EFFECT_DOWNGRADE
        await usage.set_mode(
            session, cycle=cycle, currency=currency, mode=usage.MODE_PAUSED
        )
        card = BudgetCard(
            cycle=cycle, text=self._card_text(currency, policy, total, rate_limited)
        )
        await self._io.send_budget_card(self._owner_inbox, card)
        return None

    def _warn_text(
        self, currency: str, policy: CurrencyPolicy, total: float, fraction: float
    ) -> str:
        label = _CURRENCY_LABEL[currency]
        return (
            f"⚠️ Budget: {_fmt_amount(currency, total)} of "
            f"{_fmt_amount(currency, policy.cap)} {label} used ({fraction:.0%})."
        )

    def _downgrade_text(
        self, currency: str, policy: CurrencyPolicy, total: float
    ) -> str:
        label = _CURRENCY_LABEL[currency]
        return (
            f"⚡ Budget reached: {_fmt_amount(currency, total)} of "
            f"{_fmt_amount(currency, policy.cap)} {label}. "
            "Downgrading routed categories to Copilot for this cycle."
        )

    def _card_text(
        self, currency: str, policy: CurrencyPolicy, total: float, rate_limited: bool
    ) -> str:
        label = _CURRENCY_LABEL[currency]
        if rate_limited:
            head = "🛑 Rate limited by the API — pausing to avoid burning quota."
        else:
            head = (
                f"🛑 Budget reached: {_fmt_amount(currency, total)} of "
                f"{_fmt_amount(currency, policy.cap)} {label} used."
            )
        return f"{head} Pick how to continue:"
