"""Shared fixtures: per-test sqlite db, fake settings."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.persistence import db
from chief.persistence.models import Base


@pytest_asyncio.fixture
async def session_factory(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A sessionmaker on a fresh sqlite **file** with the full schema created.

    Built from the production engine helper (:func:`chief.persistence.db.create_engine`,
    which pins NullPool), so a test session behaves like a production one: its own
    connection, its own transaction.

    NOT StaticPool-on-``:memory:``, which is what it used to be. StaticPool hands every
    concurrent session the SAME dbapi connection, putting them in ONE transaction — so
    any session's ``close()`` (pool ``reset_on_return='rollback'``) discards another
    session's in-flight write. That silently rolled back ``claim_replay``'s
    ``delivered=True`` update mid-turn and re-delivered an already-replayed message on
    the next attach (#134). A file db is the only way concurrent sessions can isolate,
    and isolation is the property the code under test actually relies on.

    The db gets its own directory (``tmp_path_factory``, not ``tmp_path``): tests point
    real dirs — ``subagents_dir``, skills — at ``tmp_path`` and assert on what is in
    them, so dropping a db file there would corrupt their fixtures.
    """
    engine = db.create_engine(str(tmp_path_factory.mktemp("db") / "test.db"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield db.session_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A single AsyncSession from the per-test ``session_factory``."""
    async with session_factory() as session:
        yield session
