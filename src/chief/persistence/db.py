"""Async sqlite engine + session factory.

Single-node personal app: one sqlite file, async access via aiosqlite so DB calls
never block the event loop that drives the adapters and the SDK.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from .models import Base


def create_engine(db_path: str) -> AsyncEngine:
    """Build the async engine for the sqlite file at ``db_path``.

    NullPool opens a fresh connection per operation rather than pooling, so a
    connection is never reused across event loops (schema init runs in a throwaway
    loop, request handling in the long-poll loop) — which would otherwise error.
    """
    return create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)


async def init_db(engine: AsyncEngine) -> None:
    """Create any missing tables (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a sessionmaker bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False)
