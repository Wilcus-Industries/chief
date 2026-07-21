"""Installer plumbing: wizard steps, service rendering, update flow."""

import asyncio
import json
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from chief.hooks.context import TurnContext
from chief.install import updatecheck
from chief.install.commands import ensure_config, main
from chief.install.lifecycle import uninstall
from chief.install.service import ServiceManager
from chief.install.units import default_path_env, launchd_plist, systemd_unit
from chief.install.update import update
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
    rc = main(["compact", "+15551234567", "--socket", str(tmp_path / "no.sock")])
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


def _update_repo(tmp_path: Path) -> ServiceManager:
    """A service manager with an installed unit, so update tries a restart."""
    manager = ServiceManager(
        platform="linux", home=tmp_path, runner=FakeRunner(), uid=1000
    )
    manager.unit_path.parent.mkdir(parents=True, exist_ok=True)
    manager.unit_path.write_text("unit")
    return manager


SHAS = {"rev-parse HEAD": "old\n", "rev-parse origin/main": "new\n"}
# `merge-base --is-ancestor origin/main HEAD` exits 0 when origin/main is
# already contained in HEAD — nothing to do. FakeRunner returns 0 by default,
# so a test that wants "there IS an update" must make that probe fail.
NEEDS_UPDATE = {"merge-base --is-ancestor": "not an ancestor"}


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


def test_update_merges_origin_main_and_restarts(tmp_path: Path) -> None:
    """Merge, never checkout: self-edit means the install always carries local
    commits, and a tag checkout would throw them out of the working tree."""
    runner = FakeRunner(stdout=SHAS, fails=NEEDS_UPDATE)
    manager = _update_repo(tmp_path)
    said: list[str] = []
    assert (
        update(
            repo_dir=tmp_path,
            runner=runner,
            service=manager,
            say=said.append,
            healthy=lambda: True,
        )
        == 0
    )
    joined = [" ".join(c) for c in runner.calls]
    assert any("merge -X theirs --no-edit origin/main" in c for c in joined)
    assert not any("checkout" in c for c in joined)
    assert any("uv sync" in c for c in joined)
    assert "autostart service restarted." in said


def test_update_no_op_when_already_at_origin_main(tmp_path: Path) -> None:
    runner = FakeRunner(stdout=SHAS)  # is-ancestor returns 0 = contained
    said: list[str] = []
    assert (
        update(
            repo_dir=tmp_path,
            runner=runner,
            service=_update_repo(tmp_path),
            say=said.append,
            healthy=lambda: True,
        )
        == 0
    )
    assert not any("merge -X" in " ".join(c) for c in runner.calls)
    assert any("already up to date" in line for line in said)


def test_update_refuses_a_dirty_tracked_tree(tmp_path: Path) -> None:
    """Untracked installed skills are normal and must not block; modified
    tracked files would be silently clobbered by -X theirs, so refuse."""
    runner = FakeRunner(
        stdout={**SHAS, "status --porcelain": " M src/x.py\n"},
        fails=NEEDS_UPDATE,
    )
    said: list[str] = []
    assert (
        update(
            repo_dir=tmp_path,
            runner=runner,
            service=_update_repo(tmp_path),
            say=said.append,
            healthy=lambda: True,
        )
        == 1
    )
    assert not any("merge -X" in " ".join(c) for c in runner.calls)
    assert any("uncommitted" in line for line in said)


def test_update_aborts_a_conflicted_merge(tmp_path: Path) -> None:
    runner = FakeRunner(
        stdout=SHAS, fails={**NEEDS_UPDATE, "merge -X theirs": "CONFLICT"}
    )
    said: list[str] = []
    assert (
        update(
            repo_dir=tmp_path,
            runner=runner,
            service=_update_repo(tmp_path),
            say=said.append,
            healthy=lambda: True,
        )
        == 1
    )
    assert any("merge --abort" in " ".join(c) for c in runner.calls)


def test_update_rolls_back_when_the_daemon_comes_up_unhealthy(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(stdout=SHAS, fails=NEEDS_UPDATE)
    said: list[str] = []
    assert (
        update(
            repo_dir=tmp_path,
            runner=runner,
            service=_update_repo(tmp_path),
            say=said.append,
            healthy=lambda: False,
        )
        == 1
    )
    joined = [" ".join(c) for c in runner.calls]
    assert any("reset --hard old" in c for c in joined)
    assert any("rolled back" in line for line in said)


def test_update_resyncs_installed_skills_from_packages(tmp_path: Path) -> None:
    """Installed skills are copies. Without this they silently go stale — a
    package skill edit deploys as code but never reaches the agent."""
    pkg = tmp_path / "packages" / "build-imessage" / "skills" / "imsg"
    pkg.mkdir(parents=True)
    (pkg / "SKILL.md").write_text("new text")
    installed = tmp_path / "skills" / "imsg"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("stale text")
    # A packaged skill that was never installed stays uninstalled.
    other = tmp_path / "packages" / "maps" / "skills" / "maps"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("maps")
    said: list[str] = []
    # `git show <before>:packages/…` returns what the installed copy holds,
    # proving it is an untouched copy and therefore safe to advance.
    runner = FakeRunner(
        stdout={**SHAS, "show old:": "stale text"}, fails=NEEDS_UPDATE
    )
    assert (
        update(
            repo_dir=tmp_path,
            runner=runner,
            service=_update_repo(tmp_path),
            say=said.append,
            healthy=lambda: True,
        )
        == 0
    )
    assert (installed / "SKILL.md").read_text() == "new text"
    assert not (tmp_path / "skills" / "maps").exists()
    assert any("imsg" in line for line in said)
    # The re-synced copy is committed, or this command's own dirty-tree guard
    # would refuse the next run (skills/ is tracked on a real install).
    assert any("commit" in " ".join(c) for c in runner.calls)


def test_update_never_clobbers_a_self_edited_skill(tmp_path: Path) -> None:
    """chief self-edits its own installed skills. An installed copy that no
    longer matches the pre-update packaged file is such an edit — advancing it
    would destroy the agent's work, so report drift and leave it alone."""
    pkg = tmp_path / "packages" / "screening" / "skills" / "screening"
    pkg.mkdir(parents=True)
    (pkg / "SKILL.md").write_text("packaged v2")
    installed = tmp_path / "skills" / "screening"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("chief's own edit")
    said: list[str] = []
    assert (
        update(
            repo_dir=tmp_path,
            # git show returns the ORIGINAL packaged text, which is not what
            # the installed copy holds — so the copy was locally edited.
            runner=FakeRunner(
                stdout={**SHAS, "show old:": "packaged v1"}, fails=NEEDS_UPDATE
            ),
            service=_update_repo(tmp_path),
            say=said.append,
            healthy=lambda: True,
        )
        == 0
    )
    assert (installed / "SKILL.md").read_text() == "chief's own edit"
    assert any("NOT overwritten" in line and "screening" in line for line in said)


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


def _cache(tmp_path: Path, *, behind: int, age: float = 0.0) -> Path:
    path = tmp_path / "update_status.json"
    path.write_text(
        json.dumps(
            {
                "behind": behind,
                "target": "abc1234",
                "checked_at": time.time() - age,
            }
        )
    )
    return path


def _fake_git(
    monkeypatch: pytest.MonkeyPatch, *, behind: str, fetch_code: int = 0
) -> list[list[str]]:
    """Stand in for the three git calls refresh() makes; record the argv."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "fetch" in cmd:
            return subprocess.CompletedProcess(cmd, fetch_code, "", "boom")
        out = behind if "rev-list" in cmd else "deadbee"
        return subprocess.CompletedProcess(cmd, 0, f"{out}\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


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
    assert updatecheck.notice(UpdateStatus(0, "abc1234", time.time())) is None


def test_notice_counts_commits_and_names_the_target() -> None:
    line = updatecheck.notice(UpdateStatus(3, "abc1234", time.time()))
    assert line is not None
    assert "3 commits" in line and "abc1234" in line
    assert "chief update" in line  # the owner runs it, not chief


def test_read_status_treats_corrupt_or_missing_cache_as_no_news(
    tmp_path: Path,
) -> None:
    assert updatecheck.read_status(tmp_path / "nope.json") is None
    corrupt = tmp_path / "update_status.json"
    corrupt.write_text("{not json")
    assert updatecheck.read_status(corrupt) is None
    corrupt.write_text('{"behind": 1}')  # well-formed JSON, wrong shape
    assert updatecheck.read_status(corrupt) is None


def test_refresh_counts_and_writes_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, behind="4")
    path = tmp_path / "update_status.json"
    status = updatecheck.refresh(tmp_path, status_path=path)
    assert status is not None
    assert (status.behind, status.target) == (4, "deadbee")
    assert json.loads(path.read_text())["behind"] == 4


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
    _fake_git(monkeypatch, behind="4", fetch_code=1)
    assert updatecheck.refresh(tmp_path, status_path=path) is None
    assert not path.exists()


async def test_session_start_answers_from_cache_without_touching_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updatecheck, "_refreshing", False)
    calls = _fake_git(monkeypatch, behind="9")
    hook = updatecheck.session_start_notice(
        tmp_path, status_path=_cache(tmp_path, behind=2)
    )
    line = await hook(_turn())
    assert line is not None and "2 commits" in line
    await _drain()
    assert calls == []  # fresh cache: no refresh scheduled at all


async def test_stale_cache_still_answers_now_and_refreshes_behind_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updatecheck, "_refreshing", False)
    _fake_git(monkeypatch, behind="9")
    path = _cache(tmp_path, behind=2, age=updatecheck.STALE_AFTER_SECONDS + 1)
    hook = updatecheck.session_start_notice(tmp_path, status_path=path)
    line = await hook(_turn())
    assert line is not None and "2 commits" in line  # the OLD answer, instantly
    await _drain()
    assert json.loads(path.read_text())["behind"] == 9  # next session sees 9


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


def test_check_updates_command_reports_without_changing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _fake_git(monkeypatch, behind="1")
    assert main(["check-updates", "--repo", str(tmp_path)]) == 0
    assert "1 commit behind" in capsys.readouterr().out


def test_check_updates_command_fails_loudly_when_git_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _fake_git(monkeypatch, behind="1", fetch_code=1)
    assert main(["check-updates", "--repo", str(tmp_path)]) == 1
    assert "could not check" in capsys.readouterr().out
