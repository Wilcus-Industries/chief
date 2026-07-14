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


MOM = "+15550000002"
OTHER = "+15550000003"


async def test_active_watches_for_handle_matches_armed_unexpired_after_floor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="tell me about dinner",
            expiry=now + timedelta(days=1),
        )
    async with session_factory() as session:
        matched = await repo.active_watches_for_handle(
            session, MOM, now=now, arrived_at=now + timedelta(minutes=1)
        )
    assert [w.id for w in matched] == [watch.id]


async def test_active_watches_for_handle_excludes_pre_creation_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="x",
            expiry=now + timedelta(days=1),
        )
    async with session_factory() as session:
        matched = await repo.active_watches_for_handle(
            session, MOM, now=now, arrived_at=now - timedelta(hours=1)
        )
    assert matched == []


async def test_active_watches_for_handle_excludes_expired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="x",
            expiry=now - timedelta(days=1),
        )
    async with session_factory() as session:
        matched = await repo.active_watches_for_handle(
            session, MOM, now=now, arrived_at=now + timedelta(minutes=1)
        )
    assert matched == []


async def test_active_watches_for_handle_excludes_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="x",
            expiry=now + timedelta(days=1),
        )
    async with session_factory() as session:
        await repo.cancel_watch(session, watch.id)
    async with session_factory() as session:
        matched = await repo.active_watches_for_handle(
            session, MOM, now=now, arrived_at=now + timedelta(minutes=1)
        )
    assert matched == []


async def test_active_watches_for_handle_excludes_fired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="x",
            expiry=now + timedelta(days=1),
        )
    async with session_factory() as session:
        row = await repo.get_watch(session, watch.id)
        assert row is not None
        row.state = repo.STATE_FIRED
        await session.commit()
    async with session_factory() as session:
        matched = await repo.active_watches_for_handle(
            session, MOM, now=now, arrived_at=now + timedelta(minutes=1)
        )
    assert matched == []


async def test_active_watches_for_handle_scopes_to_the_handle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="x",
            expiry=now + timedelta(days=1),
        )
    async with session_factory() as session:
        matched = await repo.active_watches_for_handle(
            session, OTHER, now=now, arrived_at=now + timedelta(minutes=1)
        )
    assert matched == []


# ---- unbound-watch candidates (#168) ----------------------------------------------


async def test_create_watch_with_no_target_handle_is_unbound(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=None,
            instruction="watch for the plumber",
            expiry=repo.default_expiry(NOW),
        )
    assert watch.target_handle is None
    assert watch.confirmed_at is None
    assert watch.state == repo.STATE_ARMED


async def test_list_unbound_watches_excludes_bound_cancelled_and_expired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        unbound = await repo.create_watch(
            session,
            target_handle=None,
            instruction="unbound",
            expiry=repo.default_expiry(NOW),
        )
        await repo.create_watch(
            session,
            target_handle="+15550000001",
            instruction="bound",
            expiry=repo.default_expiry(NOW),
        )
        cancelled = await repo.create_watch(
            session,
            target_handle=None,
            instruction="unbound but cancelled",
            expiry=repo.default_expiry(NOW),
        )
        await repo.cancel_watch(session, cancelled.id)
        await repo.create_watch(
            session,
            target_handle=None,
            instruction="unbound but expired",
            expiry=NOW - timedelta(days=1),
        )
    async with session_factory() as session:
        rows = await repo.list_unbound_watches(session, now=NOW)
    assert [w.id for w in rows] == [unbound.id]


async def test_get_and_create_candidate_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=None,
            instruction="x",
            expiry=repo.default_expiry(NOW),
        )
        assert await repo.get_candidate(session, watch.id, "+15550000009") is None
        created = await repo.create_candidate(
            session, watch_id=watch.id, handle="+1 (555) 000-0009", first_seen=NOW
        )
    assert created.handle == "+15550000009"
    assert created.decision == repo.CANDIDATE_PENDING
    async with session_factory() as session:
        found = await repo.get_candidate(session, watch.id, "+15550000009")
    assert found is not None and found.id == created.id


async def test_confirm_candidate_yes_binds_the_watch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=None,
            instruction="x",
            expiry=repo.default_expiry(NOW),
        )
        candidate = await repo.create_candidate(
            session, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    async with session_factory() as session:
        result = await repo.confirm_candidate(
            session, candidate.id, confirm=True, now=NOW
        )
    assert result is not None
    bound_watch, decided = result
    assert bound_watch.target_handle == "+15550000009"
    assert bound_watch.confirmed_at is not None
    assert decided.decision == repo.CANDIDATE_CONFIRMED
    # Field-for-field the same shape as a directly-created bound watch.
    async with session_factory() as session:
        directly_created = await repo.create_watch(
            session,
            target_handle="+15550000009",
            instruction="x",
            expiry=repo.default_expiry(NOW),
        )
    assert bound_watch.state == directly_created.state == repo.STATE_ARMED
    assert type(bound_watch.target_handle) is type(directly_created.target_handle)


async def test_confirm_candidate_no_leaves_target_handle_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=None,
            instruction="x",
            expiry=repo.default_expiry(NOW),
        )
        candidate = await repo.create_candidate(
            session, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    async with session_factory() as session:
        result = await repo.confirm_candidate(
            session, candidate.id, confirm=False, now=NOW
        )
    assert result is not None
    rejected_watch, decided = result
    assert rejected_watch.target_handle is None
    assert decided.decision == repo.CANDIDATE_REJECTED


async def test_confirm_candidate_returns_none_for_missing_or_already_decided(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await repo.confirm_candidate(session, 999, confirm=True, now=NOW) is None
        watch = await repo.create_watch(
            session,
            target_handle=None,
            instruction="x",
            expiry=repo.default_expiry(NOW),
        )
        candidate = await repo.create_candidate(
            session, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    async with session_factory() as session:
        await repo.confirm_candidate(session, candidate.id, confirm=True, now=NOW)
    async with session_factory() as session:
        assert (
            await repo.confirm_candidate(session, candidate.id, confirm=True, now=NOW)
            is None
        )


async def test_confirm_candidate_refuses_once_its_watch_has_expired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Proves 'watch expiry kills pending candidates' end-to-end: confirm can no
    longer succeed once the watch it belongs to has expired."""
    soon = NOW + timedelta(hours=1)
    async with session_factory() as session:
        watch = await repo.create_watch(
            session, target_handle=None, instruction="x", expiry=soon
        )
        candidate = await repo.create_candidate(
            session, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    later = soon + timedelta(hours=1)
    async with session_factory() as session:
        result = await repo.confirm_candidate(
            session, candidate.id, confirm=True, now=later
        )
    assert result is None
    async with session_factory() as session:
        row = await repo.get_watch(session, watch.id)
    assert row is not None and row.target_handle is None


async def test_confirm_candidate_refuses_once_its_watch_is_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=None,
            instruction="x",
            expiry=repo.default_expiry(NOW),
        )
        candidate = await repo.create_candidate(
            session, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
        await repo.cancel_watch(session, watch.id)
    async with session_factory() as session:
        result = await repo.confirm_candidate(
            session, candidate.id, confirm=True, now=NOW
        )
    assert result is None


async def test_list_pending_candidates_excludes_expired_watch_candidates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    soon = NOW + timedelta(hours=1)
    async with session_factory() as session:
        live_watch = await repo.create_watch(
            session,
            target_handle=None,
            instruction="live",
            expiry=repo.default_expiry(NOW),
        )
        live_candidate = await repo.create_candidate(
            session, watch_id=live_watch.id, handle="+15550000009", first_seen=NOW
        )
        expiring_watch = await repo.create_watch(
            session, target_handle=None, instruction="expiring", expiry=soon
        )
        await repo.create_candidate(
            session,
            watch_id=expiring_watch.id,
            handle="+15550000008",
            first_seen=NOW,
        )
    later = soon + timedelta(hours=1)
    async with session_factory() as session:
        pending = await repo.list_pending_candidates(session, now=later)
    assert [c.id for c in pending] == [live_candidate.id]


# ---- outbound-guard predicate + single-fire retire (#167) -------------------------


async def test_authorizing_watches_matches_armed_unexpired_on_the_handle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="x",
            expiry=datetime.now(UTC) + timedelta(days=1),
        )
    # The guard's ``now`` is the real wall clock at send time, always at or after
    # the watch's created_at floor — a just-created watch authorizes a send.
    async with session_factory() as session:
        matched = await repo.authorizing_watches(
            session, MOM, now=datetime.now(UTC)
        )
    assert [w.id for w in matched] == [watch.id]


async def test_authorizing_watches_excludes_expired_cancelled_and_other_handle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        expired = await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="expired",
            expiry=now - timedelta(days=1),
        )
        cancelled = await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="cancelled",
            expiry=now + timedelta(days=1),
        )
        await repo.cancel_watch(session, cancelled.id)
        await repo.create_watch(
            session,
            target_handle=OTHER,
            instruction="other handle",
            expiry=now + timedelta(days=1),
        )
    _ = expired
    async with session_factory() as session:
        matched = await repo.authorizing_watches(session, MOM, now=now)
    assert matched == []


async def test_retire_watch_flips_armed_to_fired_and_drops_authorization(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="x",
            expiry=now + timedelta(days=1),
        )
    async with session_factory() as session:
        retired = await repo.retire_watch(session, watch.id)
    assert retired is not None and retired.state == repo.STATE_FIRED
    # A fired watch no longer authorizes a send.
    async with session_factory() as session:
        matched = await repo.authorizing_watches(session, MOM, now=now)
    assert matched == []


async def test_retire_watch_returns_none_for_a_non_armed_watch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="x",
            expiry=now + timedelta(days=1),
        )
        await repo.cancel_watch(session, watch.id)
    async with session_factory() as session:
        assert await repo.retire_watch(session, watch.id) is None
    # Idempotent: retiring a missing watch is also a no-op.
    async with session_factory() as session:
        assert await repo.retire_watch(session, 999999) is None
