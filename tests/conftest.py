"""Shared fixtures.

``store`` gives each test its own sqlite *file* on the production engine
(NullPool). Do not "optimize" this to StaticPool / :memory: — a pooled
fixture shares one connection across concurrent sessions (#134).
"""

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
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
def sock_path() -> Iterator[Path]:
    """A short AF_UNIX socket path.

    macOS caps ``sun_path`` at ~104 bytes, and pytest's ``tmp_path`` lives
    under ``/private/var/folders/...`` which overflows it (``OSError: AF_UNIX
    path too long``). Bind under a short temp root so the socket-backed tests
    run on macOS — the app's native platform — as well as Linux.
    """
    root = "/tmp" if Path("/tmp").is_dir() else tempfile.gettempdir()
    directory = tempfile.mkdtemp(prefix="chief-", dir=root)
    try:
        yield Path(directory) / "s.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def store(engine: AsyncEngine) -> MessageStore:
    return MessageStore(make_session_factory(engine))
