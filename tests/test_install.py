"""Installer plumbing: wizard steps, service rendering, the cached update check.

The update *flow* itself lives in ``test_update.py``, against real git repos.
What is left here is the cached-verdict plumbing — a hook that must answer
without touching the network, however git misbehaves.
"""

import asyncio
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from chief.hooks.context import TurnContext
from chief.install import updatecheck, wizard_steps
from chief.install.commands import ensure_config, main
from chief.install.lifecycle import uninstall
from chief.install.service import ServiceManager
from chief.install.units import default_path_env, launchd_plist, systemd_unit
from chief.install.updatecheck import UpdateStatus
from chief.install.wizard import WizardIO, run_wizard

CONFIG = "budget:\n  cap_usd: 0\n  warn_ratio: 0.8\n"


def test_ensure_config_creates_from_template_when_missing(tmp_path: Path) -> None:
    template = tmp_path / "config.default.yaml"
    template.write_text(CONFIG)
    config = tmp_path / "config.yaml"
    assert ensure_config(config, template) is True
    assert config.read_text() == CONFIG


def test_compact_subcommand_reports_a_dead_daemon(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No daemon listening on the socket → the one-shot client fails cleanly
    # with a non-zero exit rather than a traceback.
    # Use /tmp directly: pytest's deep tmp_path exceeds macOS's 104-char AF_UNIX limit.
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        sock_path = os.path.join(td, "no.sock")
    rc = main(["compact", "+15551234567", "--socket", sock_path])
    assert rc == 1
    assert "is chief running?" in capsys.readouterr().err


def test_ensure_config_keeps_existing_config(tmp_path: Path) -> None:
    # config.yaml is install-local state: the wizard edits it in place, so a
    # re-run must never clobber the owner's configured values.
    template = tmp_path / "config.default.yaml"
    template.write_text(CONFIG)
    config = tmp_path / "config.yaml"
    config.write_text("budget:\n  cap_usd: 25\n")
    assert ensure_config(config, template) is False
    assert "cap_usd: 25" in config.read_text()


def make_io(
    answers: list[str] | None = None, secrets: list[str] | None = None
) -> tuple[WizardIO, list[str]]:
    said: list[str] = []
    prompts = iter(answers or [])
    hidden = iter(secrets or [])
    return (
        WizardIO(
            prompt=lambda _: next(prompts),
            prompt_secret=lambda _: next(hidden),
            say=said.append,
        ),
        said,
    )


def test_wizard_offers_an_update_schedule(tmp_path: Path) -> None:
    """Install offers it; chief owns it from then on."""
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG + 'update:\n  autonomy: clean-only\n  schedule: ""\n')
    io, said = make_io(answers=["25", ""], secrets=["p" * 8, "p" * 8, "sk-or-a"])
    result = run_wizard(
        secrets_dir=tmp_path / "secrets",
        config_path=config,
        io=io,
        interactive=True,
        env={},
        validate=lambda key: None,
    )
    assert result.auto_update == "set"
    assert f'schedule: "{wizard_steps.DEFAULT_UPDATE_SPEC}"' in config.read_text()
    assert "autonomy: clean-only" in config.read_text()  # untouched
    assert any("auto-update" in line for line in said)


def test_wizard_declined_update_schedule_stays_off(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG + 'update:\n  schedule: ""\n')
    io, _ = make_io(answers=["25", "n"], secrets=["p" * 8, "p" * 8, "sk-or-a"])
    result = run_wizard(
        secrets_dir=tmp_path / "secrets",
        config_path=config,
        io=io,
        interactive=True,
        env={},
        validate=lambda key: None,
    )
    assert result.auto_update == "skipped"
    assert 'schedule: ""' in config.read_text()


def test_wizard_keeps_an_existing_update_schedule(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG + 'update:\n  schedule: "0 4 * * *"\n')
    io, _ = make_io()
    result = run_wizard(
        secrets_dir=tmp_path / "secrets",
        config_path=config,
        io=io,
        interactive=False,
        env={},
        validate=lambda key: None,
    )
    assert result.auto_update == "kept"
    assert '"0 4 * * *"' in config.read_text()


def test_wizard_update_schedule_reads_env_headless(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG + 'update:\n  schedule: ""\n')
    io, _ = make_io()
    result = run_wizard(
        secrets_dir=tmp_path / "secrets",
        config_path=config,
        io=io,
        interactive=False,
        env={"CHIEF_UPDATE_SCHEDULE": "30 2 * * 0"},
        validate=lambda key: None,
    )
    assert result.auto_update == "set"
    assert 'schedule: "30 2 * * 0"' in config.read_text()


def test_wizard_interactive_sets_everything(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG)
    io, _ = make_io(answers=["25"], secrets=["hunter22", "hunter22", "sk-or-abc"])
    result = run_wizard(
        secrets_dir=tmp_path / "secrets",
        config_path=config,
        io=io,
        interactive=True,
        env={},
        validate=lambda key: None,
    )
    assert (result.password, result.model_auth, result.budget) == (
        "set",
        "set",
        "set",
    )
    assert (tmp_path / "secrets" / "web_password").read_text() == "hunter22\n"
    assert (tmp_path / "secrets" / "openrouter_api_key").read_text() == "sk-or-abc\n"
    assert "cap_usd: 25" in config.read_text()
    assert "warn_ratio: 0.8" in config.read_text()


def test_wizard_rerun_keeps_existing_state(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("budget:\n  cap_usd: 25\n")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "web_password").write_text("x")
    (secrets / "openrouter_api_key").write_text("y")
    io, _ = make_io()
    result = run_wizard(
        secrets_dir=secrets,
        config_path=config,
        io=io,
        interactive=True,
        env={},
        validate=lambda key: "should never be called",
    )
    assert (result.password, result.model_auth, result.budget) == (
        "kept",
        "kept",
        "kept",
    )


def test_wizard_headless_reads_env(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG)
    io, _ = make_io()
    result = run_wizard(
        secrets_dir=tmp_path / "secrets",
        config_path=config,
        io=io,
        interactive=False,
        env={
            "CHIEF_OWNER_PASSWORD": "longenough",
            "CHIEF_OPENROUTER_KEY": "sk-or-env",
            "CHIEF_BUDGET_CAP": "10",
        },
        validate=lambda key: None,
    )
    assert (result.password, result.model_auth, result.budget) == (
        "set",
        "set",
        "set",
    )
    assert "cap_usd: 10" in config.read_text()


def test_wizard_rejected_key_retries(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("budget:\n  cap_usd: 5\n")
    verdicts = iter(["OpenRouter rejected the key", None])
    io, said = make_io(secrets=["pw12345678", "pw12345678", "bad", "good"])
    result = run_wizard(
        secrets_dir=tmp_path / "secrets",
        config_path=config,
        io=io,
        interactive=True,
        env={},
        validate=lambda key: next(verdicts),
    )
    assert result.model_auth == "set"
    assert (tmp_path / "secrets" / "openrouter_api_key").read_text() == "good\n"
    assert any("rejected" in line for line in said)


def test_service_definitions_wrap_the_launcher(tmp_path: Path) -> None:
    launcher = tmp_path / "bin" / "chief"
    path_env = default_path_env(launcher)
    assert path_env.startswith(str(launcher.parent))
    unit = systemd_unit(launcher=launcher, repo_dir=tmp_path, path_env=path_env)
    assert f"ExecStart={launcher} run" in unit
    assert f"WorkingDirectory={tmp_path}" in unit
    plist = launchd_plist(launcher=launcher, repo_dir=tmp_path, path_env=path_env)
    assert str(launcher) in plist
    assert "com.chief.daemon" in plist


class FakeRunner:
    """Records argv calls; scripted stdout/returncode per command substring."""

    def __init__(
        self,
        stdout: dict[str, str] | None = None,
        fails: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._stdout = stdout or {}
        self._fails = fails or {}

    def __call__(self, argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, err in self._fails.items():
            if key in joined:
                return subprocess.CompletedProcess(list(argv), 1, "", err)
        for key, out in self._stdout.items():
            if key in joined:
                return subprocess.CompletedProcess(list(argv), 0, out, "")
        return subprocess.CompletedProcess(list(argv), 0, "", "")


def test_linux_service_install_writes_unit_and_enables(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = ServiceManager(
        platform="linux", home=tmp_path, runner=runner, uid=1000
    )
    manager.install(repo_dir=tmp_path, launcher=tmp_path / "chief")
    assert manager.unit_path.is_file()
    joined = [" ".join(c) for c in runner.calls]
    assert "systemctl --user daemon-reload" in joined
    assert "systemctl --user enable --now chief.service" in joined


def test_darwin_restart_kickstarts_without_booting_out(tmp_path: Path) -> None:
    """`kickstart -k` is atomic. A bootout first leaves the draining daemon
    half-holding the label, the bootstrap that follows fails, and chief stays
    down while the caller sees success — a real outage, once."""
    runner = FakeRunner()
    manager = ServiceManager(
        platform="darwin", home=tmp_path, runner=runner, uid=501
    )
    manager.restart()
    joined = [" ".join(c) for c in runner.calls]
    assert joined == ["launchctl kickstart -k gui/501/com.chief.daemon"]
    assert not any("bootout" in c for c in joined)


def test_darwin_restart_bootstraps_when_the_label_is_gone(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(fails={"kickstart -k": "no such service"})
    manager = ServiceManager(
        platform="darwin", home=tmp_path, runner=runner, uid=501
    )
    manager.restart()
    joined = [" ".join(c) for c in runner.calls]
    assert any("bootstrap gui/501" in c for c in joined)


def test_uninstall_purge_needs_confirmation(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = ServiceManager(
        platform="linux", home=tmp_path, runner=runner, uid=1000
    )
    (tmp_path / "data").mkdir()
    said: list[str] = []
    code = uninstall(
        service=manager,
        launcher=tmp_path / "chief",
        repo_dir=tmp_path,
        purge_data=True,
        assume_yes=False,
        confirm=lambda _: "n",
        say=said.append,
    )
    assert code == 1
    assert (tmp_path / "data").is_dir()


# --- update check -----------------------------------------------------------
# The hook must never put git between the owner and a reply: it answers from
# the cache and refreshes behind the turn. Every test below pins one half of
# that split, or a way git can fail without the daemon noticing.


def _cache(
    tmp_path: Path, *, current: str, latest: str, age: float = 0.0
) -> Path:
    path = tmp_path / "update_status.json"
    path.write_text(
        json.dumps(
            {
                "current": current,
                "latest": latest,
                "checked_at": time.time() - age,
            }
        )
    )
    return path


def _turn() -> TurnContext:
    return TurnContext(
        user_text="hi", messages=[], sender="owner", thread_key="t", channel="cli"
    )


async def _drain() -> None:
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)




def test_notice_is_silent_when_there_is_no_news() -> None:
    assert updatecheck.notice(None) is None
    assert updatecheck.notice(UpdateStatus("v0.4.0", "v0.4.0", time.time())) is None


def test_notice_names_both_releases_and_tells_chief_it_updates_itself() -> None:
    """It used to say the owner runs `chief update` and chief never does. That
    is the behaviour this whole system inverts."""
    line = updatecheck.notice(UpdateStatus("v0.3.0", "v0.4.0", time.time()))
    assert line is not None
    assert "v0.4.0" in line and "v0.3.0" in line
    assert "self-update" in line
    assert "never update yourself" not in line


def test_notice_handles_a_box_with_no_recorded_release() -> None:
    line = updatecheck.notice(UpdateStatus("", "v0.4.0", time.time()))
    assert line is not None and "unrecorded" in line


def test_read_status_treats_corrupt_or_missing_cache_as_no_news(
    tmp_path: Path,
) -> None:
    assert updatecheck.read_status(tmp_path / "nope.json") is None
    corrupt = tmp_path / "update_status.json"
    corrupt.write_text("{not json")
    assert updatecheck.read_status(corrupt) is None
    corrupt.write_text('{"latest": "v1.0.0"}')  # well-formed JSON, wrong shape
    assert updatecheck.read_status(corrupt) is None


def test_refresh_returns_none_when_git_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unbounded git call once wedged the daemon; a slow one must read as
    # "no news", never as an exception escaping into the caller.
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 20)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert updatecheck.refresh(tmp_path, status_path=tmp_path / "s.json") is None


def test_refresh_returns_none_when_fetch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "update_status.json"

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert updatecheck.refresh(tmp_path, status_path=path) is None
    assert not path.exists()


async def test_session_start_answers_from_cache_without_touching_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updatecheck, "_refreshing", False)
    calls: list[list[str]] = []

    def record(cmd: list[str], **kwargs: object) -> None:
        calls.append(cmd)

    monkeypatch.setattr(subprocess, "run", record)
    hook = updatecheck.session_start_notice(
        tmp_path, status_path=_cache(tmp_path, current="v0.3.0", latest="v0.4.0")
    )
    line = await hook(_turn())
    assert line is not None and "v0.4.0" in line
    await _drain()
    assert calls == []  # fresh cache: no refresh scheduled at all


async def test_concurrent_sessions_do_not_stampede_the_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updatecheck, "_refreshing", False)
    refreshes: list[Path] = []

    def counting_refresh(
        repo_dir: Path, *, status_path: Path
    ) -> UpdateStatus | None:
        refreshes.append(repo_dir)
        return None

    monkeypatch.setattr(updatecheck, "refresh", counting_refresh)
    hook = updatecheck.session_start_notice(
        tmp_path, status_path=tmp_path / "missing.json"
    )
    assert await hook(_turn()) is None
    assert await hook(_turn()) is None
    await _drain()
    assert len(refreshes) == 1
