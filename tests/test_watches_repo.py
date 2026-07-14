"""Watches repository (chief.persistence.watches): CRUD + effective_state (#165)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.persistence import watches as repo
from chief.persistence.models import Watch

NOW = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)


async def test_create_watch_normalizes_handle_and_defaults(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle="+1 (555) 000-0001",
            instruction="tell her I'll be late",
            expiry=repo.default_expiry(NOW),
        )
    assert watch.target_handle == "+15550000001"
    assert watch.expiry.replace(tzinfo=UTC) == repo.default_expiry(NOW)
    assert watch.tone == repo.TONE_REPORT
    assert watch.state == repo.STATE_ARMED


async def test_default_expiry_is_14_days_out() -> None:
    assert repo.default_expiry(NOW) == NOW + timedelta(days=14)


async def test_list_watches_orders_newest_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        first = await repo.create_watch(
            session,
            target_handle="+15550000001",
            instruction="first",
            expiry=repo.default_expiry(NOW),
        )
        second = await repo.create_watch(
            session,
            target_handle="+15550000002",
            instruction="second",
            expiry=repo.default_expiry(NOW),
        )
    async with session_factory() as session:
        rows = await repo.list_watches(session)
    assert [w.id for w in rows] == [second.id, first.id]


async def test_cancel_watch_cancels_an_armed_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle="+15550000001",
            instruction="x",
            expiry=repo.default_expiry(NOW),
        )
    async with session_factory() as session:
        cancelled = await repo.cancel_watch(session, watch.id)
    assert cancelled is not None
    assert cancelled.state == repo.STATE_CANCELLED
    async with session_factory() as session:
        row = await repo.get_watch(session, watch.id)
    assert row is not None and row.state == repo.STATE_CANCELLED


async def test_cancel_watch_missing_or_already_cancelled_is_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await repo.cancel_watch(session, 999) is None
        watch = await repo.create_watch(
            session,
            target_handle="+15550000001",
            instruction="x",
            expiry=repo.default_expiry(NOW),
        )
    async with session_factory() as session:
        await repo.cancel_watch(session, watch.id)
    async with session_factory() as session:
        assert await repo.cancel_watch(session, watch.id) is None


def test_effective_state_expires_an_armed_watch_past_its_expiry() -> None:
    armed_past = Watch(
        target_handle="+15550000001",
        instruction="x",
        expiry=NOW - timedelta(days=1),
    )
    armed_past.state = repo.STATE_ARMED
    assert repo.effective_state(armed_past, now=NOW) == repo.STATE_EXPIRED

    armed_future = Watch(
        target_handle="+15550000001",
        instruction="x",
        expiry=NOW + timedelta(days=1),
    )
    armed_future.state = repo.STATE_ARMED
    assert repo.effective_state(armed_future, now=NOW) == repo.STATE_ARMED


def test_effective_state_leaves_cancelled_alone_even_if_past_expiry() -> None:
    cancelled = Watch(
        target_handle="+15550000001",
        instruction="x",
        expiry=NOW - timedelta(days=1),
    )
    cancelled.state = repo.STATE_CANCELLED
    assert repo.effective_state(cancelled, now=NOW) == repo.STATE_CANCELLED
