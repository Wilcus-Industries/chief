"""Async SQLite engine setup.

NullPool is deliberate: a pooled sqlite fixture shares one connection (and so
one transaction) across concurrent sessions, which masked a real concurrency
bug (#134). Production and tests run the same engine configuration.
"""

from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from chief.persistence.models import Base

SessionFactory = async_sessionmaker[AsyncSession]


def make_engine(db_path: Path) -> AsyncEngine:
    """Create the async sqlite engine for the given database file."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Session factory bound to the engine; expire_on_commit off for asyncio."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_schema(engine: AsyncEngine) -> None:
    """Create all tables that don't exist yet (fresh schema, no migrations)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
