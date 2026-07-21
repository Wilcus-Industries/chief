"""Async SQLite engine setup.

NullPool is deliberate: a pooled sqlite fixture shares one connection (and so
one transaction) across concurrent sessions, which masked a real concurrency
bug (#134). Production and tests run the same engine configuration.
"""

import logging
from pathlib import Path

from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from chief.persistence.models import Base

log = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


def make_engine(db_path: Path) -> AsyncEngine:
    """Create the async sqlite engine for the given database file."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Session factory bound to the engine; expire_on_commit off for asyncio."""
    return async_sessionmaker(engine, expire_on_commit=False)


def _reconcile_columns(conn: Connection) -> None:
    """Add missing nullable columns to tables that already exist.

    ``create_all`` only creates absent tables; it never alters an existing one.
    A self-edit / in-place update that adds a column to a model would then be
    missing from a database created before the change and crash every query
    against that table (the mini's ``schedules.command`` crash, 2026-07-20).
    We add back the additive-safe columns so such a change survives an in-place
    update without a data reset — no Alembic, just ``ADD COLUMN``. A missing
    NOT NULL column can't be added to populated data, so it's logged and skipped
    rather than raising; the fresh-schema path already covers new installs.
    """
    inspector = inspect(conn)
    existing = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable:
                log.warning(
                    "schema drift: %s.%s missing and NOT NULL; cannot add to "
                    "populated table, skipping",
                    table.name,
                    column.name,
                )
                continue
            coltype = column.type.compile(dialect=conn.dialect)
            conn.execute(
                text(
                    f'ALTER TABLE "{table.name}" '
                    f'ADD COLUMN "{column.name}" {coltype}'
                )
            )
            log.info("schema: added %s.%s (%s)", table.name, column.name, coltype)


async def init_schema(engine: AsyncEngine) -> None:
    """Create absent tables, then additively reconcile columns (no migrations)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_reconcile_columns)
