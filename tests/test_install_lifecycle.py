"""Unit tests for the post-install lifecycle (#154): update, uninstall, health.

`chief update` must jump to the newest *tagged release* (never a branch tip), run
migrations on the new code, and restart the service; `chief uninstall` removes the
service + launcher and only purges data when explicitly asked. All process work
goes through the injected runner — no real git/uv/systemctl here. The health wait
is exercised against a real local HTTP listener (the probe is the mechanism).
"""

import http.server
import socket
import subprocess
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from chief.install.lifecycle import (
    main,
    resolve_db_path,
    uninstall,
    update,
    wait_for_health,
)
from chief.install.service import SYSTEMD_UNIT_NAME, ServiceManager
from test_install_service import FakeRunner


class GitFakeRunner(FakeRunner):
    """A FakeRunner with canned stdout per command prefix (git plumbing)."""

    def __init__(
        self,
        stdouts: dict[str, str] | None = None,
        failures: dict[str, int] | None = None,
    ) -> None:
        super().__init__(failures=failures)
        self.stdouts = stdouts or {}

    def __call__(
        self, argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        result = super().__call__(argv)
        joined = " ".join(argv)
        for prefix, stdout in self.stdouts.items():
            if joined.startswith(prefix):
                return subprocess.CompletedProcess(
                    result.args, result.returncode, stdout=stdout, stderr=""
                )
        return result


def _service(home: Path, runner: FakeRunner) -> ServiceManager:
    return ServiceManager(platform="linux", home=home, runner=runner, uid=1000)


def _tag_stdouts(repo: Path, *, latest: str, current: str) -> dict[str, str]:
    return {
        f"git -C {repo} tag --sort=-v:refname": f"{latest}\nv0.1.0\n",
        f"git -C {repo} describe --tags --exact-match HEAD": f"{current}\n",
    }


def test_update_checks_out_latest_tag_syncs_and_restarts(tmp_path: Path) -> None:
    repo = tmp_path / "chief"
    runner = GitFakeRunner(
        stdouts=_tag_stdouts(repo, latest="v0.2.0", current="v0.1.0")
    )
    service = _service(tmp_path, runner)
    service.definition_path.parent.mkdir(parents=True)
    service.definition_path.write_text("unit")
    said: list[str] = []

    code = update(repo_dir=repo, runner=runner, service=service, say=said.append)

    assert code == 0
    git = ["git", "-C", str(repo)]
    assert [*git, "fetch", "--tags", "--force", "origin"] in runner.calls
    assert [*git, "checkout", "v0.2.0"] in runner.calls
    assert [*git, "submodule", "update", "--init", "--recursive"] in runner.calls
    assert ["uv", "sync"] in runner.calls
    # Migrations run in a FRESH interpreter so they use the new tree's code.
    assert ["uv", "run", "python", "-m", "chief.install", "migrate"] in runner.calls
    assert ["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME] in runner.calls
    assert ["systemctl", "--user", "start", SYSTEMD_UNIT_NAME] in runner.calls
    assert any("v0.2.0" in line for line in said)


def test_update_is_a_noop_when_already_on_latest_tag(tmp_path: Path) -> None:
    repo = tmp_path / "chief"
    runner = GitFakeRunner(
        stdouts=_tag_stdouts(repo, latest="v0.2.0", current="v0.2.0")
    )
    said: list[str] = []

    code = update(
        repo_dir=repo,
        runner=runner,
        service=_service(tmp_path, runner),
        say=said.append,
    )

    assert code == 0
    assert not any("checkout" in " ".join(c) for c in runner.calls)
    assert ["uv", "sync"] not in runner.calls
    assert any("up to date" in line for line in said)


def test_update_without_tags_fails_loudly(tmp_path: Path) -> None:
    repo = tmp_path / "chief"
    runner = GitFakeRunner(
        stdouts={f"git -C {repo} tag --sort=-v:refname": "\n"}
    )
    said: list[str] = []

    code = update(
        repo_dir=repo,
        runner=runner,
        service=_service(tmp_path, runner),
        say=said.append,
    )

    assert code == 1
    assert any("no release tags" in line for line in said)


def test_update_reports_checkout_conflicts(tmp_path: Path) -> None:
    """Local edits that block the checkout surface as guidance, not a crash."""
    repo = tmp_path / "chief"
    runner = GitFakeRunner(
        stdouts=_tag_stdouts(repo, latest="v0.2.0", current="v0.1.0"),
        failures={f"git -C {repo} checkout": 1},
    )
    said: list[str] = []

    code = update(
        repo_dir=repo,
        runner=runner,
        service=_service(tmp_path, runner),
        say=said.append,
    )

    assert code == 1
    assert ["uv", "sync"] not in runner.calls
    assert any("local changes" in line for line in said)


def test_uninstall_removes_service_and_launcher(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    service.install(repo_dir=tmp_path / "repo", launcher=tmp_path / "chief")
    launcher = tmp_path / "bin" / "chief"
    launcher.parent.mkdir()
    launcher.write_text("#!/bin/bash\n")
    data_dir = tmp_path / "repo" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "chief.db").write_text("x")

    code = uninstall(
        service=service,
        launcher=launcher,
        repo_dir=tmp_path / "repo",
        secrets_dir=tmp_path / "secrets",
        purge_data=False,
        assume_yes=True,
    )

    assert code == 0
    assert not service.installed
    assert not launcher.exists()
    assert data_dir.exists(), "data must survive without --purge-data"


def test_uninstall_purge_data_removes_data_and_secrets(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    launcher = tmp_path / "bin" / "chief"
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "web_password").write_text("scrypt$...")

    code = uninstall(
        service=service,
        launcher=launcher,
        repo_dir=repo,
        secrets_dir=secrets,
        purge_data=True,
        assume_yes=True,
    )

    assert code == 0
    assert not (repo / "data").exists()
    assert not secrets.exists()


def test_uninstall_purge_asks_first_and_no_aborts(tmp_path: Path) -> None:
    runner = FakeRunner()
    secrets = tmp_path / "secrets"
    secrets.mkdir()

    code = uninstall(
        service=_service(tmp_path, runner),
        launcher=tmp_path / "bin" / "chief",
        repo_dir=tmp_path / "repo",
        secrets_dir=secrets,
        purge_data=True,
        assume_yes=False,
        confirm=lambda _msg: "n",
    )

    assert code == 1
    assert secrets.exists(), "a declined purge must not delete anything"


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def local_http() -> Iterator[int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()


def test_wait_for_health_succeeds_against_live_listener(local_http: int) -> None:
    assert wait_for_health(f"http://127.0.0.1:{local_http}/", timeout=5.0)


def test_wait_for_health_times_out_when_nothing_listens() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    assert not wait_for_health(f"http://127.0.0.1:{port}/", timeout=0.7)


def test_resolve_db_path_env_wins_over_config(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("db_path: data/from-config.db\n")

    assert (
        resolve_db_path(env={"DB_PATH": "/tmp/x.db"}, config_path=config)
        == "/tmp/x.db"
    )
    assert (
        resolve_db_path(env={}, config_path=config) == "data/from-config.db"
    )
    assert (
        resolve_db_path(env={}, config_path=tmp_path / "missing.yaml")
        == "data/chief.db"
    )


def test_main_migrate_creates_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`python -m chief.install migrate` upgrades a fresh db to head."""
    db_path = tmp_path / "nested" / "chief.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    assert main(["migrate"]) == 0
    assert db_path.is_file()
    # Idempotent: a second run is a no-op success, like alembic upgrade head.
    assert main(["migrate"]) == 0
