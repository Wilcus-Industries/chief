"""Shared fixtures.

``store`` gives each test its own sqlite *file* on the production engine
(NullPool). Do not "optimize" this to StaticPool / :memory: — a pooled
fixture shares one connection across concurrent sessions (#134).

``_isolate_installed_packages`` keeps ``build_app`` tests off the operator's
real install state; see its docstring — without it the suite's result depends
on which packages happen to be installed on the box running it.
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


@pytest.fixture(autouse=True)
def _isolate_installed_packages(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point ``build_app``'s package discovery at empty dirs.

    ``chief.hooks.boot.build_hooks`` resolves the installed registry and the cloned
    package dir from cwd-relative module constants, so a test booting the real
    app loaded whatever the *operator* had installed. That made the suite's
    result a function of the box: green on a dev clone with no
    ``data/installed.yaml``, red on a deployed daemon whose installed hooks
    fire during a turn and consume the ``FakeProvider`` script. It bit for real
    — the obsidian-memory relevance gate went from one model call per firing to
    one per candidate, and eight `test_app` tests went red on the deployed box
    only, blocking self-edit (whose gate is the done-check).

    Tests that want a package loaded build the library explicitly with their
    own roots, so nothing legitimately depends on these constants.
    """
    empty = tmp_path_factory.mktemp("no-packages")
    monkeypatch.setattr("chief.hooks.boot.CLONED_PACKAGES_DIR", empty / "cloned")
    monkeypatch.setattr(
        "chief.hooks.boot.INSTALLED_REGISTRY", empty / "installed.yaml"
    )


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
