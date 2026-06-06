"""BudgetGate (M9) — cycle key, threshold warnings, exhaustion pause+card."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.base import BudgetCard
from chief.core.budget import BudgetGate, cycle_key
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
    credit: float = 100.0,
    warn_fractions: tuple[float, ...] = (0.75, 0.90),
    exhaust_fraction: float = 1.0,
    now: datetime | None = None,
) -> BudgetGate:
    fixed = now or _at(year=2026, month=6, day=10)
    return BudgetGate(
        session_factory=session_factory,
        io=io,
        owner_inbox="owner-inbox",
        monthly_credit_usd=credit,
        warn_fractions=warn_fractions,
        exhaust_fraction=exhaust_fraction,
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


# ---- record: warnings ----------------------------------------------------


async def test_record_below_threshold_is_silent(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    await gate.record(50.0)

    assert io.sends == []
    assert io.cards == []
    assert await gate.mode() == usage.MODE_NORMAL


async def test_record_crossing_warn_tier_warns_once(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    await gate.record(80.0)  # 0.80 ≥ 0.75 tier
    await gate.record(1.0)  # 0.81, still below 0.90 → no new warn

    assert len(io.sends) == 1
    assert io.sends[0][0] == "owner-inbox"
    assert io.cards == []


async def test_record_crossing_second_tier_warns_again(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    await gate.record(80.0)  # crosses 0.75
    await gate.record(12.0)  # 0.92 crosses 0.90

    assert len(io.sends) == 2


async def test_record_jump_to_exhaustion_cards_without_warning(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    await gate.record(100.0)  # straight to 1.0 — card, no separate tier warning

    assert io.sends == []
    assert io.cards == [("owner-inbox", "2026-06")]
    assert await gate.mode() == usage.MODE_PAUSED


async def test_exhaustion_cards_only_once(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    await gate.record(100.0)
    await gate.record(5.0)  # already paused — no second card

    assert len(io.cards) == 1


async def test_record_after_owner_continues_does_not_repause(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    async with session_factory() as s:
        await usage.set_mode(s, cycle="2026-06", mode=usage.MODE_CONTINUE)

    await gate.record(150.0)  # well past exhaustion

    assert io.cards == []
    assert await gate.mode() == usage.MODE_CONTINUE


async def test_mode_defaults_normal_with_no_row(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    assert await gate.mode() == usage.MODE_NORMAL


async def test_concurrent_records_card_only_once(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    # Two turns finishing at once both cross exhaustion; the gate lock serializes the
    # read-decide-write so only the first pauses + cards (the second sees mode!=normal).
    gate = _gate(session_factory, io)

    await asyncio.gather(gate.record(100.0), gate.record(100.0))

    assert len(io.cards) == 1
    assert await gate.mode() == usage.MODE_PAUSED


# ---- note_rate_limited ---------------------------------------------------


async def test_note_rate_limited_pauses_and_cards(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    await gate.note_rate_limited()

    assert io.cards == [("owner-inbox", "2026-06")]
    assert await gate.mode() == usage.MODE_PAUSED


async def test_note_rate_limited_noop_when_not_normal(
    session_factory: async_sessionmaker[AsyncSession], io: FakeBudgetIO
) -> None:
    gate = _gate(session_factory, io)
    async with session_factory() as s:
        await usage.set_mode(s, cycle="2026-06", mode=usage.MODE_OVERFLOW)

    await gate.note_rate_limited()

    assert io.cards == []
    assert await gate.mode() == usage.MODE_OVERFLOW
