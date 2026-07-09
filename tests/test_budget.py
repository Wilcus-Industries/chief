"""Per-currency BudgetGate (#84): meter, warn, pause premium / downgrade dollars."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.base import BudgetCard
from chief.core.budget import (
    ACCUM_ADD,
    ACCUM_MAX,
    ACTION_DOWNGRADE,
    ACTION_NONE,
    ACTION_PAUSE,
    EFFECT_DOWNGRADE,
    BudgetGate,
    CurrencyPolicy,
    cycle_key,
    premium_request_total,
)
from chief.persistence import usage


class FakeBudgetIO:
    """Records owner-inbox sends and budget cards (the slice BudgetGate delivers to)."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, str]] = []
        self.cards: list[tuple[str, str]] = []  # (route, cycle)

    async def send(self, thread_key: str, text: str) -> None:
        self.sends.append((thread_key, text))

    async def send_budget_card(self, route: str, card: BudgetCard) -> None:
        self.cards.append((route, card.cycle))


def _at(*, year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=UTC)


def _gate(
    session_factory: async_sessionmaker[AsyncSession],
    io: FakeBudgetIO,
    *,
    premium_cap: float = 200.0,
    dollar_cap: float = 100.0,
    warn_fractions: tuple[float, ...] = (0.75, 0.90),
    exhaust_fraction: float = 1.0,
    now: datetime | None = None,
) -> BudgetGate:
    fixed = now or _at(year=2026, month=6, day=10)
    policies = {
        usage.PREMIUM_REQUESTS: CurrencyPolicy(
            cap=premium_cap,
            warn_fractions=warn_fractions,
            exhaust_fraction=exhaust_fraction,
            accumulation=ACCUM_MAX,
            action=ACTION_PAUSE,
        ),
        usage.OPENROUTER_DOLLARS: CurrencyPolicy(
            cap=dollar_cap,
            warn_fractions=warn_fractions,
            exhaust_fraction=exhaust_fraction,
            accumulation=ACCUM_ADD,
            action=ACTION_DOWNGRADE,
        ),
        usage.BRIDGE_TURNS: CurrencyPolicy(
            cap=float("inf"),
            warn_fractions=(),
            exhaust_fraction=1.0,
            accumulation=ACCUM_ADD,
            action=ACTION_NONE,
        ),
    }
    return BudgetGate(
        session_factory=session_factory,
        io=io,
        owner_inbox="owner-inbox",
        policies=policies,
        owner_tz="UTC",
        anchor_day=1,
        now=lambda: fixed,
    )


@pytest_asyncio.fixture
async def io() -> AsyncIterator[FakeBudgetIO]:
    yield FakeBudgetIO()


# ---- cycle_key (pure) ----------------------------------------------------


def test_cycle_key_calendar_month() -> None:
    tz = ZoneInfo("UTC")
    assert cycle_key(_at(year=2026, month=6, day=10), tz, 1) == "2026-06"


def test_cycle_key_before_anchor_belongs_to_prior_month() -> None:
    tz = ZoneInfo("UTC")
    # anchor_day 15: the 10th is still inside the cycle that started May 15.
    assert cycle_key(_at(year=2026, month=6, day=10), tz, 15) == "2026-05"
    assert cycle_key(_at(year=2026, month=6, day=20), tz, 15) == "2026-06"


def test_cycle_key_january_rolls_year_back() -> None:
    tz = ZoneInfo("UTC")
    assert cycle_key(_at(year=2026, month=1, day=5), tz, 15) == "2025-12"


def test_cycle_key_uses_local_timezone() -> None:
    # 2026-06-01 03:00 UTC is still May 31 in Los Angeles → prior calendar month.
    la = ZoneInfo("America/Los_Angeles")
    at = datetime(2026, 6, 1, 3, 0, tzinfo=UTC)
    assert cycle_key(at, la, 1) == "2026-05"


# ---- premium_request_total (pure) ---------------------------------------


def test_premium_request_total_sums_and_defaults_zero() -> None:
    assert premium_request_total({}) == 0.0
    assert premium_request_total({"premium": 3}) == 3.0
    assert premium_request_total({"premium": 3, "chat": 1}) == 4.0


# ---- premium requests: cumulative accumulate + warn + pause -------------


async def test_premium_accumulates_monotonically_against_cap(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io, premium_cap=200.0)
    await gate.record(usage.PREMIUM_REQUESTS, 40)  # 20% — silent
    await gate.record(usage.PREMIUM_REQUESTS, 40)  # a lower/stale snapshot, no growth

    assert io.sends == []
    async with session_factory() as s:
        row = await usage.get_row(s, "2026-06", usage.PREMIUM_REQUESTS)
    assert row is not None and row.amount == 40  # monotonic: never double-counted


async def test_premium_crossing_warn_tier_warns_once(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io, premium_cap=200.0)
    await gate.record(usage.PREMIUM_REQUESTS, 160)  # 0.80 ≥ 0.75 tier
    await gate.record(usage.PREMIUM_REQUESTS, 165)  # 0.825, still < 0.90 → no new warn

    assert len(io.sends) == 1
    assert io.sends[0][0] == "owner-inbox"
    assert io.cards == []


async def test_premium_exhaustion_pauses_and_cards(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io, premium_cap=200.0)
    effect = await gate.record(usage.PREMIUM_REQUESTS, 200)  # straight to the cap

    assert effect is None  # premium pauses, it does not signal a downgrade
    assert io.cards == [("owner-inbox", "2026-06")]
    assert await gate.mode(usage.PREMIUM_REQUESTS) == usage.MODE_PAUSED


async def test_premium_exhaustion_cards_only_once(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io, premium_cap=200.0)
    await gate.record(usage.PREMIUM_REQUESTS, 200)
    await gate.record(usage.PREMIUM_REQUESTS, 210)  # already paused — no second card

    assert len(io.cards) == 1


# ---- OpenRouter dollars: additive accumulate + warn + downgrade ---------


async def test_dollars_accumulate_against_cap_and_warn(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io, dollar_cap=100.0)
    await gate.record(usage.OPENROUTER_DOLLARS, 50.0)  # 0.50 — silent
    await gate.record(usage.OPENROUTER_DOLLARS, 30.0)  # 0.80 ≥ 0.75 → one warn

    assert len(io.sends) == 1
    async with session_factory() as s:
        row = await usage.get_row(s, "2026-06", usage.OPENROUTER_DOLLARS)
    assert row is not None and row.amount == 80.0  # additive


async def test_dollars_exhaustion_downgrades_without_a_card(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io, dollar_cap=100.0)
    effect = await gate.record(usage.OPENROUTER_DOLLARS, 100.0)

    # Dollars downgrade (re-target openrouter → Copilot), signalled to the engine —
    # never a pause card. The owner is notified, not gated.
    assert effect == EFFECT_DOWNGRADE
    assert io.cards == []
    assert len(io.sends) == 1
    assert await gate.mode(usage.OPENROUTER_DOLLARS) == usage.MODE_DOWNGRADED


async def test_dollars_downgrade_signals_only_once(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io, dollar_cap=100.0)
    assert await gate.record(usage.OPENROUTER_DOLLARS, 100.0) == EFFECT_DOWNGRADE
    # Already downgraded — a further over-budget turn must not re-signal or re-notify.
    assert await gate.record(usage.OPENROUTER_DOLLARS, 10.0) is None
    assert len(io.sends) == 1


# ---- bridge turns: informational only -----------------------------------


async def test_bridge_turns_count_without_warning_or_action(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    for _ in range(500):
        assert await gate.record(usage.BRIDGE_TURNS, 1.0) is None

    assert io.sends == []
    assert io.cards == []
    async with session_factory() as s:
        row = await usage.get_row(s, "2026-06", usage.BRIDGE_TURNS)
    assert row is not None and row.amount == 500  # counted, never budgeted


async def test_mode_defaults_normal_with_no_row(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    assert await gate.mode(usage.PREMIUM_REQUESTS) == usage.MODE_NORMAL


async def test_concurrent_premium_records_card_only_once(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    # Two turns finishing at once both cross exhaustion; the gate lock serializes the
    # read-decide-write so only the first pauses + cards (the second sees mode!=normal).
    gate = _gate(session_factory, io, premium_cap=200.0)

    await asyncio.gather(
        gate.record(usage.PREMIUM_REQUESTS, 200),
        gate.record(usage.PREMIUM_REQUESTS, 200),
    )

    assert len(io.cards) == 1
    assert await gate.mode(usage.PREMIUM_REQUESTS) == usage.MODE_PAUSED


# ---- note_rate_limited ---------------------------------------------------


async def test_note_rate_limited_pauses_the_quota_currency(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    effect = await gate.note_rate_limited(usage.PREMIUM_REQUESTS)

    assert effect is None
    assert io.cards == [("owner-inbox", "2026-06")]
    assert await gate.mode(usage.PREMIUM_REQUESTS) == usage.MODE_PAUSED


async def test_note_rate_limited_downgrades_the_dollar_currency(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    effect = await gate.note_rate_limited(usage.OPENROUTER_DOLLARS)

    assert effect == EFFECT_DOWNGRADE
    assert io.cards == []
    assert await gate.mode(usage.OPENROUTER_DOLLARS) == usage.MODE_DOWNGRADED


async def test_note_rate_limited_noop_for_bridge(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    assert await gate.note_rate_limited(usage.BRIDGE_TURNS) is None
    assert io.cards == [] and io.sends == []


async def test_note_rate_limited_noop_when_not_normal(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    async with session_factory() as s:
        await usage.set_mode(
            s,
            cycle="2026-06",
            currency=usage.PREMIUM_REQUESTS,
            mode=usage.MODE_OVERFLOW,
        )

    await gate.note_rate_limited(usage.PREMIUM_REQUESTS)

    assert io.cards == []
    assert await gate.mode(usage.PREMIUM_REQUESTS) == usage.MODE_OVERFLOW
