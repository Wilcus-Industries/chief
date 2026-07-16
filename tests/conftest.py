"""Shared fixtures.

``store`` gives each test its own sqlite *file* on the production engine
(NullPool). Do not "optimize" this to StaticPool / :memory: — a pooled
fixture shares one connection across concurrent sessions (#134).
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chief.persistence.db import init_schema, make_engine, make_session_factory
from chief.persistence.store import MessageStore


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(tmp_path / "chief.db")
    await init_schema(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
def store(engine: AsyncEngine) -> MessageStore:
    return MessageStore(make_session_factory(engine))
