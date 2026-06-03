"""Schema creation and the contact + task repositories."""

from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import AsyncSession

from chief.persistence import tasks as task_repo
from chief.persistence.contacts import get_or_create_contact


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
    assert contact.first_seen is not None


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
