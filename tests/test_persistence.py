"""Schema creation and the contact repository."""

from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import AsyncSession

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
