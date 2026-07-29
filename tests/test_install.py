"""Installer plumbing: wizard steps, service rendering, the cached update check.

The update *flow* itself lives in ``test_update.py``, against real git repos.
What is left here is the cached-verdict plumbing — a hook that must answer
without touching the network, however git misbehaves.
"""

import asyncio
import getpass
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from chief.config.write import set_dedicated_mode
from chief.hooks.context import TurnContext
from chief.install import updatecheck, wizard_steps
from chief.install.account import Step, account_plan, default_home
from chief.install.commands import ensure_config, main
from chief.install.dedicated import existing_home, setup_account
from chief.install.lifecycle import uninstall
from chief.install.posture import ON, UNKNOWN, Posture, chief_account, read_posture
from chief.install.service import ServiceManager
from chief.install.session import (
    AUTO_LOGIN,
    NO_SESSION,
    SCREEN_SHARING,
    SessionPlan,
    disk_encrypted,
    password_conflict,
    session_plan,
)
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


# --- dedicated account plan (#286)


def test_darwin_account_plan_is_pinned() -> None:
    plan = account_plan(
        platform="darwin", owner="owner", password="hunter2", email="c@l"
    )
    assert [list(step.command()) for step in plan.steps] == [
        ["sudo", "sysadminctl", "-addUser", "chief", "-fullName", "chief",
         "-home", "/Users/chief", "-shell", "/bin/zsh", "-password", "-"],
        ["sudo", "dseditgroup", "-o", "create", "chief"],
        ["sudo", "dseditgroup", "-o", "edit", "-a", "chief", "-t", "user",
         "chief"],
        ["sudo", "dseditgroup", "-o", "edit", "-a", "owner", "-t", "user",
         "chief"],
        ["sudo", "chown", "-R", "chief:chief", "/opt/chief"],
        ["sudo", "chmod", "-R", "g+rwX", "/opt/chief"],
        ["sudo", "find", "/opt/chief", "-type", "d", "-exec", "chmod", "g+s",
         "{}", "+"],
        ["sudo", "chmod", "-R", "go-rwx", "/opt/chief/secrets"],
        ["sudo", "-u", "chief", "git", "config", "--global", "user.name",
         "chief"],
        ["sudo", "-u", "chief", "git", "config", "--global", "user.email",
         "c@l"],
        ["git", "config", "--global", "--add", "safe.directory", "/opt/chief"],
    ]
    assert plan.home == Path("/Users/chief")


def test_linux_account_plan_is_pinned() -> None:
    plan = account_plan(
        platform="linux", owner="owner", password="hunter2", email="c@l"
    )
    assert [list(step.command()) for step in plan.steps] == [
        ["sudo", "useradd", "--create-home", "--home-dir", "/home/chief",
         "--shell", "/bin/bash", "chief"],
        ["sudo", "chpasswd"],
        ["sudo", "groupadd", "--force", "chief"],
        ["sudo", "usermod", "-aG", "chief", "chief"],
        ["sudo", "usermod", "-aG", "chief", "owner"],
        ["sudo", "chown", "-R", "chief:chief", "/opt/chief"],
        ["sudo", "chmod", "-R", "g+rwX", "/opt/chief"],
        ["sudo", "find", "/opt/chief", "-type", "d", "-exec", "chmod", "g+s",
         "{}", "+"],
        ["sudo", "chmod", "-R", "go-rwx", "/opt/chief/secrets"],
        ["sudo", "-u", "chief", "git", "config", "--global", "user.name",
         "chief"],
        ["sudo", "-u", "chief", "git", "config", "--global", "user.email",
         "c@l"],
        ["git", "config", "--global", "--add", "safe.directory", "/opt/chief"],
        ["sudo", "loginctl", "enable-linger", "chief"],
    ]


def test_the_account_password_never_reaches_an_argv() -> None:
    """A pinned argv is printed, logged and diffed — the password must not be
    in one. It rides stdin instead."""
    for platform in ("darwin", "linux"):
        plan = account_plan(platform=platform, owner="owner", password="hunter2")
        assert not any("hunter2" in arg for s in plan.steps for arg in s.command())
        assert any(s.stdin and "hunter2" in s.stdin for s in plan.steps)


def test_using_an_existing_account_skips_creation_but_keeps_the_rest() -> None:
    plan = account_plan(platform="linux", owner="owner", create=False)
    joined = [" ".join(s.command()) for s in plan.steps]
    assert not any("useradd" in c or "chpasswd" in c for c in joined)
    assert "sudo usermod -aG chief owner" in joined
    assert "sudo chmod -R go-rwx /opt/chief/secrets" in joined
    assert "sudo loginctl enable-linger chief" in joined


def test_secrets_are_carved_out_after_the_group_sweep() -> None:
    """Ordering is the whole point: a later g+rwX sweep would re-open them."""
    plan = account_plan(platform="linux", owner="owner", create=False)
    joined = [" ".join(s.command()) for s in plan.steps]
    assert joined.index("sudo chmod -R g+rwX /opt/chief") < joined.index(
        "sudo chmod -R go-rwx /opt/chief/secrets"
    )


def test_account_plan_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        account_plan(platform="plan9", owner="owner", password="x")
    with pytest.raises(ValueError, match="needs a password"):
        account_plan(platform="linux", owner="owner")


# --- login-session mechanism (#286)


def test_filevault_state_decides_the_session_mechanism() -> None:
    on = FakeRunner(stdout={"fdesetup": "FileVault is On.\n"})
    off = FakeRunner(stdout={"fdesetup": "FileVault is Off.\n"})
    assert disk_encrypted("darwin", on) is True
    assert disk_encrypted("darwin", off) is False
    assert disk_encrypted("linux", on) is None
    assert not on.calls[1:]  # one probe, no retries


def test_unreadable_filevault_state_is_treated_as_encrypted() -> None:
    """Automatic login on an encrypted disk silently does nothing, so an
    unknown answer must take the branch with the human step, not the one that
    looks like it worked."""
    broken = FakeRunner(fails={"fdesetup": "boom"})
    assert disk_encrypted("darwin", broken) is None
    plan = session_plan(
        platform="darwin", encrypted=None, user="chief", password="pw"
    )
    assert plan.mechanism == SCREEN_SHARING
    assert any("assuming encrypted" in line for line in plan.manual)


def test_unencrypted_mac_gets_pinned_auto_login() -> None:
    plan = session_plan(
        platform="darwin", encrypted=False, user="chief", password="hunter2"
    )
    assert plan.mechanism == AUTO_LOGIN
    assert [list(s.command()) for s in plan.steps] == [
        ["sudo", "sysadminctl", "-autologin", "set", "-userName", "chief",
         "-password", "-"],
    ]
    assert not any("hunter2" in a for s in plan.steps for a in s.command())
    assert plan.steps[0].stdin == "hunter2\n"


def test_encrypted_mac_gets_pinned_screen_sharing_and_a_human_step() -> None:
    plan = session_plan(platform="darwin", encrypted=True, user="chief")
    assert plan.mechanism == SCREEN_SHARING
    assert [list(s.command()) for s in plan.steps] == [
        ["sudo", "launchctl", "enable", "system/com.apple.screensharing"],
        ["sudo", "launchctl", "load", "-w",
         "/System/Library/LaunchDaemons/com.apple.screensharing.plist"],
    ]
    assert any("vnc://127.0.0.1" in line for line in plan.manual)
    assert any("log in as chief" in line for line in plan.manual)


def test_linux_needs_no_session_mechanism() -> None:
    plan = session_plan(platform="linux", encrypted=None, user="chief")
    assert plan == SessionPlan(NO_SESSION, (), ())


def test_session_plan_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        session_plan(platform="plan9", encrypted=False, user="chief")
    with pytest.raises(ValueError, match="needs chief's login password"):
        session_plan(platform="darwin", encrypted=False, user="chief")


def test_auto_login_refuses_a_password_matching_the_apple_id() -> None:
    assert password_conflict("same", "same") is not None
    assert password_conflict("login", "appleid") is None
    assert password_conflict("", "") is None


# --- boot check (#286)


def _posture_runner(*, filevault: str, auto: str | None, session: bool) -> "FakeRunner":
    fails = {} if auto is not None else {"autoLoginUser": "does not exist"}
    if not session:
        fails["launchctl print"] = "could not find service"
    return FakeRunner(
        stdout={"fdesetup": filevault, "autoLoginUser": (auto or "") + "\n"},
        fails=fails,
    )


def test_posture_reads_the_three_facts() -> None:
    runner = _posture_runner(
        filevault="FileVault is On.\n", auto=None, session=True
    )
    state = read_posture(platform="darwin", user="chief", uid=502, runner=runner)
    assert (state.encryption, state.auto_login, state.session) == (
        "on",
        "off",
        "present",
    )
    assert state.summary() == "ok"
    assert ["launchctl", "print", "gui/502"] in runner.calls


def test_posture_catches_the_silent_auto_login_breakage() -> None:
    """The whole reason the check exists: an OS update clears autoLoginUser,
    chief never comes back after the next reboot, and nothing says so."""
    runner = _posture_runner(
        filevault="FileVault is Off.\n", auto=None, session=False
    )
    state = read_posture(platform="darwin", user="chief", uid=502, runner=runner)
    problems = state.problems()
    assert any("no graphical session" in p for p in problems)
    assert any("automatic login is not set to chief" in p for p in problems)
    assert state.summary() != "ok"


def test_posture_flags_auto_login_that_an_encrypted_disk_will_ignore() -> None:
    runner = _posture_runner(
        filevault="FileVault is On.\n", auto="chief", session=True
    )
    state = read_posture(platform="darwin", user="chief", uid=502, runner=runner)
    assert any("macOS ignores it" in p for p in state.problems())


def test_posture_is_quiet_on_a_healthy_unencrypted_mac() -> None:
    runner = _posture_runner(
        filevault="FileVault is Off.\n", auto="chief", session=True
    )
    state = read_posture(platform="darwin", user="chief", uid=502, runner=runner)
    assert state.problems() == ()


def test_posture_on_linux_probes_nothing() -> None:
    runner = FakeRunner()
    state = read_posture(platform="linux", user="chief", uid=1000, runner=runner)
    assert (state.encryption, state.auto_login, state.session) == ("n/a",) * 3
    assert state.problems() == ()
    assert runner.calls == []


# --- dedicated account install flow (#286)


class RecordingRunner:
    """Records the steps it was handed; scripted failure by argv substring."""

    def __init__(self, fail: str | None = None) -> None:
        self.steps: list[Step] = []
        self._fail = fail

    def __call__(self, step: Step) -> "subprocess.CompletedProcess[str]":
        self.steps.append(step)
        joined = " ".join(step.command())
        rc = 1 if self._fail and self._fail in joined else 0
        return subprocess.CompletedProcess(list(step.command()), rc, "", "")


def _answers(*replies: str) -> tuple[WizardIO, list[str], RecordingRunner]:
    said: list[str] = []
    prompts = iter(replies)
    secrets = iter(["hunter22", "hunter22", ""])
    io = WizardIO(
        prompt=lambda _: next(prompts),
        prompt_secret=lambda _: next(secrets),
        say=said.append,
    )
    return io, said, RecordingRunner()


def test_a_scripted_run_refuses_to_create_a_system_account(tmp_path: Path) -> None:
    """An unattended installer must not add a system user behind the owner's
    back — it falls back to today's install and says so."""
    io, said, runner = _answers()
    setup = setup_account(
        platform="linux",
        owner="owner",
        home=tmp_path,
        io=io,
        interactive=False,
        execute=runner,
    )
    assert setup.mode == "refused"
    assert not setup.dedicated
    assert runner.steps == []
    assert any("will not create a system account" in line for line in said)


def test_declining_leaves_todays_single_user_install(tmp_path: Path) -> None:
    io, said, runner = _answers("n")
    setup = setup_account(
        platform="linux",
        owner="owner",
        home=tmp_path,
        io=io,
        interactive=True,
        execute=runner,
    )
    assert (setup.mode, setup.user) == ("declined", "owner")
    assert runner.steps == []
    assert any("runs as you" in line for line in said)


def test_declining_at_the_confirmation_runs_nothing(tmp_path: Path) -> None:
    io, _, runner = _answers("c", "", "", "", "n")
    setup = setup_account(
        platform="linux",
        owner="owner",
        home=tmp_path,
        io=io,
        interactive=True,
        encrypted=None,
        execute=runner,
    )
    assert setup.mode == "declined"
    assert runner.steps == []


def test_creating_the_account_runs_the_plan_and_reports_what_is_left(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("imessage:\n  enabled: false\n  mode: self\n")
    io, _, runner = _answers("c", "", "", "", "y")
    setup = setup_account(
        platform="darwin",
        owner="owner",
        home=tmp_path,
        io=io,
        interactive=True,
        tree=tmp_path / "tree",
        config_path=config,
        encrypted=True,
        execute=runner,
    )
    assert setup.dedicated and setup.user == "chief"
    assert setup.session == SCREEN_SHARING
    ran = [" ".join(s.command()) for s in runner.steps]
    assert any("sysadminctl -addUser chief" in c for c in ran)
    assert any("go-rwx" in c for c in ran)
    assert any("screensharing" in c for c in ran)
    # The four self-DM compensations only come off in dedicated mode.
    assert "mode: dedicated" in config.read_text()
    assert any("does not reach already-open shells" in m for m in setup.manual)
    assert any("not started" in m for m in setup.manual)


def test_a_failed_step_stops_the_run_rather_than_half_building(
    tmp_path: Path,
) -> None:
    prompts = iter(["c", "", "", "", "y"])
    secrets = iter(["hunter22", "hunter22", ""])
    io = WizardIO(
        prompt=lambda _: next(prompts),
        prompt_secret=lambda _: next(secrets),
        say=lambda _: None,
    )
    runner = RecordingRunner(fail="chmod -R g+rwX")
    with pytest.raises(RuntimeError, match="failed"):
        setup_account(
            platform="linux",
            owner="owner",
            home=tmp_path,
            io=io,
            interactive=True,
            tree=tmp_path / "tree",
            config_path=tmp_path / "config.yaml",
            encrypted=None,
            execute=runner,
        )
    ran = [" ".join(s.command()) for s in runner.steps]
    assert not any("go-rwx" in c for c in ran)  # nothing after the failure


def test_granted_directories_are_group_permissions_and_never_the_home_root(
    tmp_path: Path,
) -> None:
    home = tmp_path
    (home / "notes").mkdir()
    prompts = iter(["c", "", str(home), f"{home / 'notes'}", "y"])
    secrets = iter(["hunter22", "hunter22", ""])
    io = WizardIO(
        prompt=lambda _: next(prompts),
        prompt_secret=lambda _: next(secrets),
        say=lambda _: None,
    )
    runner = RecordingRunner()
    setup_account(
        platform="linux",
        owner="owner",
        home=home,
        io=io,
        interactive=True,
        tree=tmp_path / "tree",
        config_path=tmp_path / "config.yaml",
        encrypted=None,
        execute=runner,
    )
    ran = [" ".join(s.command()) for s in runner.steps]
    assert f"sudo chgrp -R chief {home / 'notes'}" in ran
    assert f"sudo chmod -R g+rwX {home / 'notes'}" in ran
    # The home root was typed at the read prompt and must have been refused.
    assert f"sudo chgrp -R chief {home}" not in ran


def test_the_report_carries_what_install_sh_branches_on(tmp_path: Path) -> None:
    io, _, runner = _answers("c", "", "", "", "y")
    setup = setup_account(
        platform="linux",
        owner="owner",
        home=tmp_path,
        io=io,
        interactive=True,
        tree=tmp_path / "tree",
        config_path=tmp_path / "config.yaml",
        encrypted=None,
        execute=runner,
    )
    report = setup.report()
    assert "mode=create" in report
    assert "user=chief" in report
    assert "home=/home/chief" in report


def test_the_tree_writes_all_land_before_the_chown_takes_it_away(
    tmp_path: Path,
) -> None:
    """The installer runs as the owner; the plan chowns the tree to chief and
    this process's group membership does not update. Anything it still has to
    write into that tree — config.yaml, the report install.sh branches on —
    must already be on disk by the time the first permission step runs, or the
    install dies mid-plan with a real account and a half-configured box."""
    config = tmp_path / "config.yaml"
    config.write_text("imessage:\n  enabled: false\n  mode: self\n")
    report = tmp_path / "account-setup"
    seen_at_chown: list[tuple[bool, bool]] = []

    def execute(step: Step) -> "subprocess.CompletedProcess[str]":
        if "chown" in step.command():
            seen_at_chown.append(
                ("mode: dedicated" in config.read_text(), report.exists())
            )
        return subprocess.CompletedProcess(list(step.command()), 0, "", "")

    io, _, _ = _answers("c", "", "", "", "y")
    setup_account(
        platform="linux",
        owner="owner",
        home=tmp_path,
        io=io,
        interactive=True,
        tree=tmp_path / "tree",
        config_path=config,
        report=report,
        encrypted=None,
        execute=execute,
    )
    assert seen_at_chown == [(True, True)]
    assert "mode=create" in report.read_text()


def test_the_mode_flip_finds_the_imessage_block_not_the_first_mode_key(
    tmp_path: Path,
) -> None:
    """config.yaml is user-ordered and package-extended: a `mode:` under any
    block above `imessage:` must not be the one that gets flipped."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "update:\n  mode: clean-only\nimessage:\n  enabled: true\n  mode: self\n"
    )
    assert set_dedicated_mode(config)
    assert config.read_text() == (
        "update:\n  mode: clean-only\nimessage:\n  enabled: true\n"
        "  mode: dedicated\n"
    )


def test_an_existing_account_keeps_its_real_home() -> None:
    """`existing_home` reads the account rather than guessing /Users/<user>:
    the launchd plist is placed by that value, and a wrong one loads nowhere.
    `root` is the discriminator — its home is never the guessed default."""
    assert existing_home("no-such-user-for-chief-tests") is None
    assert existing_home("root") not in (None, default_home("linux", "root"))
    assert existing_home("root") != default_home("darwin", "root")


def test_chief_status_probes_chiefs_account_not_the_owner_who_typed_it(
    tmp_path: Path,
) -> None:
    """The owner is the only one who ever types `chief status`, so resolving
    the posture user from the caller reports the owner's session as chief's."""
    report = tmp_path / "account-setup"
    report.write_text("mode=declined\nuser=owner\nhome=\nsession=none\n")
    assert chief_account(report) is None  # single-user install: caller is right
    report.write_text(f"mode=create\nuser={getpass.getuser()}\nhome=\n")
    assert chief_account(report) == (getpass.getuser(), os.getuid())
    assert chief_account(tmp_path / "absent") is None


def test_an_unreadable_encryption_probe_still_flags_a_doomed_auto_login() -> None:
    """`disk_encrypted` returns None for unknown and every caller must treat
    that as encrypted — `problems()` was the one that stayed quiet."""
    unknown = Posture("chief", UNKNOWN, "chief", "present")
    assert any("macOS ignores it" in p for p in unknown.problems())
    assert unknown.problems() == Posture("chief", ON, "chief", "present").problems()


def test_the_service_definition_can_be_written_without_starting_it(
    tmp_path: Path,
) -> None:
    """launchd cannot bootstrap into a session that does not exist yet: the
    dedicated install writes the plist and lets chief's first login load it."""
    runner = FakeRunner()
    manager = ServiceManager(
        platform="darwin", home=tmp_path, runner=runner, uid=502
    )
    manager.install(
        repo_dir=tmp_path, launcher=tmp_path / "chief", start=False
    )
    assert manager.plist_path.is_file()
    assert runner.calls == []


def test_uninstall_keeps_the_system_account_unless_asked(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = ServiceManager(
        platform="linux", home=tmp_path, runner=runner, uid=1000
    )
    said: list[str] = []
    steps = RecordingRunner()
    uninstall(
        service=manager,
        launcher=tmp_path / "chief",
        repo_dir=tmp_path,
        purge_data=False,
        assume_yes=True,
        confirm=lambda _: pytest.fail("must not ask with --yes"),
        say=said.append,
        execute=steps,
    )
    assert steps.steps == []
    assert any("system account kept" in line for line in said)


def test_uninstall_removes_the_account_when_asked(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = ServiceManager(
        platform="linux", home=tmp_path, runner=runner, uid=1000
    )
    steps = RecordingRunner()
    uninstall(
        service=manager,
        launcher=tmp_path / "chief",
        repo_dir=tmp_path,
        purge_data=False,
        assume_yes=True,
        remove_account=True,
        say=lambda _: None,
        execute=steps,
    )
    assert [" ".join(s.command()) for s in steps.steps] == [
        "sudo userdel --remove chief",
        "sudo groupdel --force chief",
    ]
