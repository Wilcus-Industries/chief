"""Schedule repository (chief.persistence.schedules): create, due-list, advance."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from chief.persistence import schedules as schedule_repo


def _at(minutes: int) -> datetime:
    """A UTC timestamp ``minutes`` from a fixed epoch (deterministic ordering)."""
    return datetime(2026, 6, 4, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes)


async def test_create_sets_defaults_and_roundtrips(db_session: AsyncSession) -> None:
    schedule = await schedule_repo.create_schedule(
        db_session,
        kind=schedule_repo.KIND_ONCE,
        spec="2026-06-04T12:30:00+00:00",
        action="stretch",
        action_type=schedule_repo.ACTION_MESSAGE,
        next_run=_at(30),
    )

    assert schedule.id is not None
    assert schedule.enabled is True
    assert schedule.urgent is False
    assert schedule.thread_key is None
    assert schedule.last_run is None

    fetched = await schedule_repo.get_schedule(db_session, schedule.id)
    assert fetched is not None
    assert fetched.action == "stretch"
    assert fetched.action_type == schedule_repo.ACTION_MESSAGE


async def test_get_missing_returns_none(db_session: AsyncSession) -> None:
    assert await schedule_repo.get_schedule(db_session, 999) is None


async def test_list_due_is_inclusive_and_excludes_future_and_disabled(
    db_session: AsyncSession,
) -> None:
    now = _at(0)
    # Exactly on the boundary → due (the <= boundary fires this tick).
    on_time = await schedule_repo.create_schedule(
        db_session,
        kind=schedule_repo.KIND_RECURRING,
        spec="* * * * *",
        action="now",
        action_type=schedule_repo.ACTION_WAKEUP,
        next_run=now,
    )
    # A missed past fire is still due (restart catch-up).
    overdue = await schedule_repo.create_schedule(
        db_session,
        kind=schedule_repo.KIND_ONCE,
        spec="x",
        action="late",
        action_type=schedule_repo.ACTION_MESSAGE,
        next_run=_at(-5),
    )
    # Future → not yet due.
    await schedule_repo.create_schedule(
        db_session,
        kind=schedule_repo.KIND_ONCE,
        spec="x",
        action="later",
        action_type=schedule_repo.ACTION_MESSAGE,
        next_run=_at(5),
    )
    # Disabled, even though its time has passed → excluded.
    disabled = await schedule_repo.create_schedule(
        db_session,
        kind=schedule_repo.KIND_ONCE,
        spec="x",
        action="off",
        action_type=schedule_repo.ACTION_MESSAGE,
        next_run=_at(-1),
    )
    await schedule_repo.disable_schedule(db_session, disabled)

    due = await schedule_repo.list_due(db_session, now=now)

    # Oldest first: the overdue one precedes the on-time one.
    assert [s.id for s in due] == [overdue.id, on_time.id]


async def test_set_next_run_and_last_run_persist(db_session: AsyncSession) -> None:
    schedule = await schedule_repo.create_schedule(
        db_session,
        kind=schedule_repo.KIND_RECURRING,
        spec="* * * * *",
        action="tick",
        action_type=schedule_repo.ACTION_WAKEUP,
        next_run=_at(0),
    )

    await schedule_repo.set_last_run(db_session, schedule, _at(0))
    await schedule_repo.set_next_run(db_session, schedule, _at(1))

    fetched = await schedule_repo.get_schedule(db_session, schedule.id)
    assert fetched is not None
    # sqlite round-trips naive; compare on the wall-clock value.
    assert fetched.next_run is not None
    assert fetched.next_run.replace(tzinfo=UTC) == _at(1)
    assert fetched.last_run is not None
    assert fetched.last_run.replace(tzinfo=UTC) == _at(0)


async def test_disable_drops_from_due_and_enabled(db_session: AsyncSession) -> None:
    now = _at(0)
    schedule = await schedule_repo.create_schedule(
        db_session,
        kind=schedule_repo.KIND_ONCE,
        spec="x",
        action="once",
        action_type=schedule_repo.ACTION_MESSAGE,
        next_run=now,
    )

    assert [s.id for s in await schedule_repo.list_enabled(db_session)] == [schedule.id]

    await schedule_repo.disable_schedule(db_session, schedule)

    assert await schedule_repo.list_due(db_session, now=now) == []
    assert await schedule_repo.list_enabled(db_session) == []


async def test_create_with_thread_key_and_urgent(db_session: AsyncSession) -> None:
    schedule = await schedule_repo.create_schedule(
        db_session,
        kind=schedule_repo.KIND_RECURRING,
        spec="0 2 * * *",
        action="df -h",
        action_type=schedule_repo.ACTION_BASH,
        next_run=_at(0),
        thread_key="-100:7",
        urgent=True,
    )

    fetched = await schedule_repo.get_schedule(db_session, schedule.id)
    assert fetched is not None
    assert fetched.thread_key == "-100:7"
    assert fetched.urgent is True
    assert fetched.action_type == schedule_repo.ACTION_BASH
