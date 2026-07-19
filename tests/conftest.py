"""Shared fixtures.

``store`` gives each test its own sqlite *file* on the production engine
(NullPool). Do not "optimize" this to StaticPool / :memory: — a pooled
fixture shares one connection across concurrent sessions (#134).
"""

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chief.persistence.db import init_schema, make_engine, make_session_factory
from chief.persistence.store import MessageStore

_FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


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


@pytest.fixture(scope="session")
def embedder() -> Any:
    """The model2vec static model, loaded once per test session.

    Loading downloads ~30MB of weights on first run then caches them; sharing
    one loaded model keeps every obsidian-memory test well under the 30s
    per-test timeout. Imported lazily so sessions that touch no memory test
    never pay for it.
    """
    from model2vec import StaticModel

    return StaticModel.from_pretrained("minishlab/potion-base-8M")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A throwaway copy of the fixture Obsidian vault, safe to mutate."""
    destination = tmp_path / "vault"
    shutil.copytree(_FIXTURE_VAULT, destination)
    return destination
