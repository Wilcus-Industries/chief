"""Schema-init reconciles additive columns on an existing database.

``create_all`` only creates absent tables; it never alters an existing one. A
self-edit / in-place update that adds a column to a model would then be missing
from a database created before the change and crash every query against it
(the mini's ``schedules.command`` crash, 2026-07-20). ``init_schema`` must add
back the safe (nullable) columns so an additive change survives without a reset.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from chief.persistence.db import init_schema, make_engine


def _columns(sync_conn: Connection, table: str) -> set[str]:
    return {c["name"] for c in inspect(sync_conn).get_columns(table)}


@pytest.fixture
async def legacy_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """An engine whose ``schedules`` table predates the ``command`` column."""
    engine = make_engine(tmp_path / "legacy.db")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE schedules ("
                "id INTEGER NOT NULL PRIMARY KEY, "
                "description VARCHAR NOT NULL, "
                "spec VARCHAR NOT NULL, "
                "wake_channel VARCHAR NOT NULL, "
                "wake_thread VARCHAR NOT NULL, "
                "prompt VARCHAR NOT NULL, "
                "enabled BOOLEAN NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO schedules "
                "(id, description, spec, wake_channel, wake_thread, prompt, "
                "enabled, created_at) VALUES "
                "(1, 'd', '* * * * *', 'web', 't', 'hi', 1, '2026-01-01')"
            )
        )
    yield engine
    await engine.dispose()


async def test_init_schema_adds_missing_nullable_column(
    legacy_engine: AsyncEngine,
) -> None:
    async with legacy_engine.begin() as conn:
        before = await conn.run_sync(_columns, "schedules")
    assert "command" not in before

    await init_schema(legacy_engine)

    async with legacy_engine.begin() as conn:
        after = await conn.run_sync(_columns, "schedules")
    assert "command" in after


async def test_init_schema_preserves_existing_rows(legacy_engine: AsyncEngine) -> None:
    await init_schema(legacy_engine)
    async with legacy_engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT description, command FROM schedules WHERE id = 1")
            )
        ).one()
    assert row.description == "d"
    assert row.command is None


async def test_init_schema_is_idempotent(legacy_engine: AsyncEngine) -> None:
    await init_schema(legacy_engine)
    await init_schema(legacy_engine)  # second run must not raise
    async with legacy_engine.begin() as conn:
        cols = await conn.run_sync(_columns, "schedules")
    assert "command" in cols
