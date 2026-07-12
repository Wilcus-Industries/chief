"""Schema creation and the contact + task repositories."""

from sqlalchemy import Connection, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.persistence import contacts as contact_repo
from chief.persistence import tasks as task_repo
from chief.persistence.contacts import get_or_create_contact
from chief.persistence.models import MessageLogEntry


async def test_concurrent_sessions_do_not_share_a_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two sessions open at once must isolate — production's contract (NullPool).

    The #134 defect this pins: a pooled fixture that hands every concurrent session
    the SAME dbapi connection puts them in ONE transaction, so a bystander's close()
    (pool reset_on_return='rollback') discards another session's in-flight write.
    That is what silently reverted ``claim_replay``'s delivered=True and re-delivered
    an already-replayed message on the second reattach.
    """
    async with session_factory() as writer:
        writer.add(
            MessageLogEntry(
                platform="cli",
                thread_key="cli:main",
                role="chief",
                kind="reply",
                text="hi",
            )
        )
        await writer.flush()  # in-flight, NOT committed

        async with session_factory() as bystander:
            unseen = (await bystander.execute(select(MessageLogEntry))).scalars().all()
            assert unseen == []  # a separate transaction cannot see uncommitted work

        await writer.commit()

    async with session_factory() as reader:
        rows = (await reader.execute(select(MessageLogEntry))).scalars().all()
    assert len(rows) == 1  # the bystander's close() must not have rolled it back


async def test_full_schema_is_created(db_session: AsyncSession) -> None:
    def _table_names(sync_conn: Connection) -> list[str]:
        return inspect(sync_conn).get_table_names()

    conn = await db_session.connection()
    tables = set(await conn.run_sync(_table_names))
    assert {
        "contacts",
        "tasks",
        "approvals",
        "policy",
        "rate_limits",
        "schedules",
    } <= tables


async def test_get_or_create_contact_inserts(db_session: AsyncSession) -> None:
    contact = await get_or_create_contact(
        db_session, platform="telegram", user_id="42", tier="owner", display_name="Will"
    )

    assert contact.id is not None
    assert contact.namespace == "telegram:42"
    assert contact.admitted is False
    assert contact.state == contact_repo.STATE_PENDING
    assert contact.first_seen is not None


async def test_new_contact_is_pending(db_session: AsyncSession) -> None:
    contact = await get_or_create_contact(
        db_session, platform="telegram", user_id="7", tier="guest"
    )

    assert contact.state == contact_repo.STATE_PENDING


async def test_get_contact_returns_none_when_absent(db_session: AsyncSession) -> None:
    assert (
        await contact_repo.get_contact(db_session, platform="telegram", user_id="x")
        is None
    )


async def test_set_contact_state_persists(db_session: AsyncSession) -> None:
    contact = await get_or_create_contact(
        db_session, platform="telegram", user_id="55", tier="guest"
    )

    await contact_repo.set_contact_state(
        db_session, contact, contact_repo.STATE_BLOCKED
    )

    reloaded = await contact_repo.get_contact(
        db_session, platform="telegram", user_id="55"
    )
    assert reloaded is not None
    assert reloaded.state == contact_repo.STATE_BLOCKED


async def test_find_contacts_by_name_matches_substring_case_insensitively(
    db_session: AsyncSession,
) -> None:
    await get_or_create_contact(
        db_session,
        platform="telegram",
        user_id="1",
        tier="guest",
        display_name="Alice Smith",
    )
    await get_or_create_contact(
        db_session,
        platform="telegram",
        user_id="2",
        tier="guest",
        display_name="Bob Jones",
    )
    # Different platform — must not match.
    await get_or_create_contact(
        db_session,
        platform="discord",
        user_id="3",
        tier="guest",
        display_name="Alice Other",
    )

    matches = await contact_repo.find_contacts_by_name(
        db_session, platform="telegram", name="alice"
    )

    assert [c.user_id for c in matches] == ["1"]


async def test_get_or_create_contact_is_idempotent(db_session: AsyncSession) -> None:
    first = await get_or_create_contact(
        db_session, platform="telegram", user_id="42", tier="owner"
    )
    second = await get_or_create_contact(
        db_session,
        platform="telegram",
        user_id="42",
        tier="owner",
        display_name="ignored",
    )

    assert first.id == second.id


async def test_get_or_create_task_inserts_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    first = await task_repo.get_or_create_task(
        db_session, platform="telegram", thread_key="-100:5", tier="owner", title="ship"
    )
    second = await task_repo.get_or_create_task(
        db_session, platform="telegram", thread_key="-100:5", tier="owner"
    )

    assert first.id == second.id
    assert first.status == task_repo.OPEN
    assert first.title == "ship"


async def test_get_or_create_task_backfills_a_null_surface(
    db_session: AsyncSession,
) -> None:
    """A row opened without a surface (legacy, /route, branch, Opus escalation) is
    healed by the first dispatch that knows the real one (#151)."""
    legacy = await task_repo.get_or_create_task(
        db_session, platform="telegram", thread_key="-100:51", tier="owner"
    )
    assert legacy.surface is None

    healed = await task_repo.get_or_create_task(
        db_session,
        platform="telegram",
        thread_key="-100:51",
        tier="owner",
        surface="group",
    )

    assert healed.id == legacy.id
    reloaded = await task_repo.get_task(
        db_session, platform="telegram", thread_key="-100:51"
    )
    assert reloaded is not None
    assert reloaded.surface == "group"


async def test_get_or_create_task_never_overwrites_a_proven_surface(
    db_session: AsyncSession,
) -> None:
    """Once a surface is proven, no later call may change it — a rewrite to ``dm`` on a
    GROUP task is exactly the approval-card leak #135 closed."""
    await task_repo.get_or_create_task(
        db_session,
        platform="telegram",
        thread_key="-100:52",
        tier="owner",
        surface="group",
    )

    await task_repo.get_or_create_task(
        db_session,
        platform="telegram",
        thread_key="-100:52",
        tier="owner",
        surface="dm",
    )

    reloaded = await task_repo.get_task(
        db_session, platform="telegram", thread_key="-100:52"
    )
    assert reloaded is not None
    assert reloaded.surface == "group"


async def test_get_or_create_task_leaves_surface_null_when_unproven(
    db_session: AsyncSession,
) -> None:
    """A surface-less call on a surface-less row must not invent one."""
    await task_repo.get_or_create_task(
        db_session, platform="telegram", thread_key="-100:53", tier="owner"
    )
    again = await task_repo.get_or_create_task(
        db_session, platform="telegram", thread_key="-100:53", tier="owner"
    )

    assert again.surface is None


async def test_set_status_and_session_id_persist(db_session: AsyncSession) -> None:
    task = await task_repo.get_or_create_task(
        db_session, platform="telegram", thread_key="-100:6", tier="owner"
    )

    await task_repo.set_session_id(db_session, task, "sess-abc")
    await task_repo.set_status(db_session, task, task_repo.RUNNING)

    reloaded = await task_repo.get_task(
        db_session, platform="telegram", thread_key="-100:6"
    )
    assert reloaded is not None
    assert reloaded.sdk_session_id == "sess-abc"
    assert reloaded.status == task_repo.RUNNING


async def test_set_task_model_persists_and_clears(db_session: AsyncSession) -> None:
    task = await task_repo.get_or_create_task(
        db_session, platform="telegram", thread_key="-100:9", tier="owner"
    )
    assert task.model is None

    await task_repo.set_task_model(db_session, task, "claude-opus-4-8")
    reloaded = await task_repo.get_task(
        db_session, platform="telegram", thread_key="-100:9"
    )
    assert reloaded is not None
    assert reloaded.model == "claude-opus-4-8"

    await task_repo.set_task_model(db_session, task, None)
    reloaded = await task_repo.get_task(
        db_session, platform="telegram", thread_key="-100:9"
    )
    assert reloaded is not None
    assert reloaded.model is None


async def test_list_active_excludes_terminal(db_session: AsyncSession) -> None:
    live = await task_repo.get_or_create_task(
        db_session, platform="telegram", thread_key="-100:7", tier="owner"
    )
    done = await task_repo.get_or_create_task(
        db_session, platform="telegram", thread_key="-100:8", tier="owner"
    )
    await task_repo.set_status(db_session, done, task_repo.DONE)

    active = await task_repo.list_active(db_session)

    assert [t.id for t in active] == [live.id]
