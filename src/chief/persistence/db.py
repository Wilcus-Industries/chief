"""Async sqlite engine + session factory.

Single-node personal app: one sqlite file, async access via aiosqlite so DB calls
never block the event loop that drives the adapters and the SDK.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

# alembic.ini lives at the repo root, two levels above this file.
_ALEMBIC_INI = Path(__file__).parent.parent.parent.parent / "alembic.ini"


def create_engine(db_path: str) -> AsyncEngine:
    """Build the async engine for the sqlite file at ``db_path``.

    NullPool opens a fresh connection per operation rather than pooling, so a
    connection is never reused across event loops (schema init runs in a throwaway
    loop, request handling in the long-poll loop) — which would otherwise error.
    """
    return create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)


def _run_migrations(db_path: str) -> None:
    """Run ``alembic upgrade head`` synchronously against ``db_path``.

    Alembic always runs migrations in a sync context; env.py strips the
    ``+aiosqlite`` driver prefix when building the migration engine.  Calling
    this before the async event loop starts (or in a thread) avoids event-loop
    conflicts entirely.
    """
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(cfg, "head")


async def init_db(engine: AsyncEngine) -> None:
    """Apply all pending Alembic migrations (idempotent; upgrades to head).

    Runs synchronously via the blocking Alembic command API before the async
    engine is used for queries, so there is no event-loop conflict.
    """
    # Extract the file path from the engine URL (strips the aiosqlite driver).
    db_path = str(engine.url.database or "")
    _run_migrations(db_path)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a sessionmaker bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False)
