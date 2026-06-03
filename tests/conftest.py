"""Shared fixtures: clean auth env, in-memory db, fake settings."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief.persistence.models import Base


@pytest.fixture(autouse=True)
def _no_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ANTHROPIC_API_KEY out of the env for every test by default.

    Its presence is a hard failure in config; tests that exercise that guard set
    it explicitly. Defaulting it absent keeps the rest of the suite hermetic.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A sessionmaker on a fresh in-memory sqlite with the full schema created.

    StaticPool keeps every checkout on the one in-memory connection, so schema and
    rows are visible across sessions opened from the same factory within a test.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A single AsyncSession from the in-memory ``session_factory``."""
    async with session_factory() as session:
        yield session
