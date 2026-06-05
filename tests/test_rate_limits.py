"""Fixed-window rate-limit counter (M6 guest abuse/cost guard)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from chief.persistence.rate_limits import check_and_increment

_T0 = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)


async def test_under_limit_allows_and_counts(db_session: AsyncSession) -> None:
    for _ in range(3):
        allowed = await check_and_increment(
            db_session, scope="telegram:1", window_seconds=60, limit=3, now=_T0
        )
        assert allowed is True


async def test_over_limit_rejects(db_session: AsyncSession) -> None:
    for _ in range(3):
        assert await check_and_increment(
            db_session, scope="telegram:1", window_seconds=60, limit=3, now=_T0
        )
    # 4th call within the window is over the cap.
    assert not await check_and_increment(
        db_session, scope="telegram:1", window_seconds=60, limit=3, now=_T0
    )


async def test_window_resets_after_expiry(db_session: AsyncSession) -> None:
    for _ in range(3):
        assert await check_and_increment(
            db_session, scope="telegram:1", window_seconds=60, limit=3, now=_T0
        )
    assert not await check_and_increment(
        db_session, scope="telegram:1", window_seconds=60, limit=3, now=_T0
    )
    # A call past the window starts a fresh count.
    later = _T0 + timedelta(seconds=61)
    assert await check_and_increment(
        db_session, scope="telegram:1", window_seconds=60, limit=3, now=later
    )


async def test_scopes_are_independent(db_session: AsyncSession) -> None:
    for _ in range(3):
        assert await check_and_increment(
            db_session, scope="telegram:1", window_seconds=60, limit=3, now=_T0
        )
    # A different scope (e.g. the global budget) has its own counter.
    assert await check_and_increment(
        db_session, scope="global", window_seconds=60, limit=3, now=_T0
    )
