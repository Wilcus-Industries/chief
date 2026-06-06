"""Monthly-cost repository (M9 usage budgeting) — upsert + mode/warn state."""

from sqlalchemy.ext.asyncio import AsyncSession

from chief.persistence.usage import (
    MODE_DOWNGRADED,
    MODE_NORMAL,
    add_cost,
    get_row,
    mark_warned,
    set_mode,
)


async def test_get_row_missing_returns_none(db_session: AsyncSession) -> None:
    assert await get_row(db_session, "2026-06") is None


async def test_add_cost_creates_and_accumulates(db_session: AsyncSession) -> None:
    assert await add_cost(db_session, cycle="2026-06", amount=1.5) == 1.5
    assert await add_cost(db_session, cycle="2026-06", amount=2.0) == 3.5

    row = await get_row(db_session, "2026-06")
    assert row is not None
    assert row.total_cost_usd == 3.5
    # A fresh row starts in the normal, un-warned state.
    assert row.mode == MODE_NORMAL
    assert row.warned_fraction == 0.0


async def test_cycles_are_independent(db_session: AsyncSession) -> None:
    await add_cost(db_session, cycle="2026-06", amount=5.0)
    assert await add_cost(db_session, cycle="2026-07", amount=1.0) == 1.0

    june = await get_row(db_session, "2026-06")
    assert june is not None
    assert june.total_cost_usd == 5.0


async def test_set_mode_flips_persisted_mode(db_session: AsyncSession) -> None:
    await add_cost(db_session, cycle="2026-06", amount=1.0)
    await set_mode(db_session, cycle="2026-06", mode=MODE_DOWNGRADED)

    row = await get_row(db_session, "2026-06")
    assert row is not None
    assert row.mode == MODE_DOWNGRADED


async def test_mark_warned_bumps_high_water_mark(db_session: AsyncSession) -> None:
    await add_cost(db_session, cycle="2026-06", amount=1.0)
    await mark_warned(db_session, cycle="2026-06", fraction=0.75)

    row = await get_row(db_session, "2026-06")
    assert row is not None
    assert row.warned_fraction == 0.75


async def test_mark_warned_never_lowers_the_mark(db_session: AsyncSession) -> None:
    await mark_warned(db_session, cycle="2026-06", fraction=0.9)
    # A lower (out-of-order) fraction must not re-arm an already-warned tier.
    await mark_warned(db_session, cycle="2026-06", fraction=0.75)

    row = await get_row(db_session, "2026-06")
    assert row is not None
    assert row.warned_fraction == 0.9


async def test_set_mode_creates_row_when_absent(db_session: AsyncSession) -> None:
    # The card decision can land before any cost is recorded this cycle.
    await set_mode(db_session, cycle="2026-06", mode=MODE_DOWNGRADED)

    row = await get_row(db_session, "2026-06")
    assert row is not None
    assert row.mode == MODE_DOWNGRADED
    assert row.total_cost_usd == 0.0
