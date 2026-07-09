"""Per-currency usage repository (#84) — upsert (add/raise) + mode/warn state."""

from sqlalchemy.ext.asyncio import AsyncSession

from chief.persistence.usage import (
    MODE_DOWNGRADED,
    MODE_NORMAL,
    OPENROUTER_DOLLARS,
    PREMIUM_REQUESTS,
    add_amount,
    get_row,
    mark_warned,
    raise_amount,
    set_mode,
)


async def test_get_row_missing_returns_none(db_session: AsyncSession) -> None:
    assert await get_row(db_session, "2026-06", PREMIUM_REQUESTS) is None


async def test_add_amount_creates_and_accumulates(db_session: AsyncSession) -> None:
    # OpenRouter dollars are per-turn deltas: they sum into the running total.
    d = OPENROUTER_DOLLARS
    assert await add_amount(db_session, cycle="2026-06", currency=d, amount=1.5) == 1.5
    assert await add_amount(db_session, cycle="2026-06", currency=d, amount=2.0) == 3.5

    row = await get_row(db_session, "2026-06", d)
    assert row is not None
    assert row.amount == 3.5
    # A fresh row starts in the normal, un-warned state.
    assert row.mode == MODE_NORMAL
    assert row.warned_fraction == 0.0


async def test_raise_amount_keeps_high_water_mark(db_session: AsyncSession) -> None:
    # Premium requests report a cumulative snapshot: raise is monotonic so a repeated or
    # stale reading never double-counts (or lowers) the total.
    pr = PREMIUM_REQUESTS
    assert await raise_amount(db_session, cycle="2026-06", currency=pr, amount=40) == 40
    assert await raise_amount(db_session, cycle="2026-06", currency=pr, amount=55) == 55
    # A lower (stale) reading does not lower the accumulated total.
    assert await raise_amount(db_session, cycle="2026-06", currency=pr, amount=50) == 55


async def test_currencies_are_independent(db_session: AsyncSession) -> None:
    pr = PREMIUM_REQUESTS
    await raise_amount(db_session, cycle="2026-06", currency=pr, amount=90)
    got = await add_amount(
        db_session, cycle="2026-06", currency=OPENROUTER_DOLLARS, amount=4.0
    )
    assert got == 4.0  # the dollar currency has its own row in the same cycle

    premium = await get_row(db_session, "2026-06", PREMIUM_REQUESTS)
    assert premium is not None and premium.amount == 90


async def test_cycles_are_independent(db_session: AsyncSession) -> None:
    pr = PREMIUM_REQUESTS
    await raise_amount(db_session, cycle="2026-06", currency=pr, amount=120)
    assert await raise_amount(db_session, cycle="2026-07", currency=pr, amount=10) == 10

    june = await get_row(db_session, "2026-06", pr)
    assert june is not None and june.amount == 120


async def test_set_mode_flips_persisted_mode(db_session: AsyncSession) -> None:
    dollars = OPENROUTER_DOLLARS
    await add_amount(db_session, cycle="2026-06", currency=dollars, amount=1.0)
    await set_mode(db_session, cycle="2026-06", currency=dollars, mode=MODE_DOWNGRADED)

    row = await get_row(db_session, "2026-06", dollars)
    assert row is not None
    assert row.mode == MODE_DOWNGRADED


async def test_mark_warned_bumps_high_water_mark(db_session: AsyncSession) -> None:
    pr = PREMIUM_REQUESTS
    await raise_amount(db_session, cycle="2026-06", currency=pr, amount=150)
    await mark_warned(db_session, cycle="2026-06", currency=pr, fraction=0.75)

    row = await get_row(db_session, "2026-06", pr)
    assert row is not None
    assert row.warned_fraction == 0.75


async def test_mark_warned_never_lowers_the_mark(db_session: AsyncSession) -> None:
    pr = PREMIUM_REQUESTS
    await mark_warned(db_session, cycle="2026-06", currency=pr, fraction=0.9)
    # A lower (out-of-order) fraction must not re-arm an already-warned tier.
    await mark_warned(db_session, cycle="2026-06", currency=pr, fraction=0.75)

    row = await get_row(db_session, "2026-06", pr)
    assert row is not None
    assert row.warned_fraction == 0.9


async def test_set_mode_creates_row_when_absent(db_session: AsyncSession) -> None:
    # The card decision can land before any usage is recorded this cycle.
    dollars = OPENROUTER_DOLLARS
    await set_mode(db_session, cycle="2026-06", currency=dollars, mode=MODE_DOWNGRADED)

    row = await get_row(db_session, "2026-06", dollars)
    assert row is not None
    assert row.mode == MODE_DOWNGRADED
    assert row.amount == 0.0
