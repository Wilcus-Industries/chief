"""Installer plumbing: wizard steps, service rendering, update flow."""

import subprocess
from collections.abc import Sequence
from pathlib import Path

from chief.install.commands import ensure_config
from chief.install.lifecycle import uninstall, update
from chief.install.service import ServiceManager
from chief.install.units import default_path_env, launchd_plist, systemd_unit
from chief.install.wizard import WizardIO, run_wizard

CONFIG = "budget:\n  cap_usd: 0\n  warn_ratio: 0.8\n"


def test_ensure_config_creates_from_template_when_missing(tmp_path: Path) -> None:
    template = tmp_path / "config.default.yaml"
    template.write_text(CONFIG)
    config = tmp_path / "config.yaml"
    assert ensure_config(config, template) is True
    assert config.read_text() == CONFIG


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


# rev-parse HEAD then origin/main: differing shas mean there is something
# to merge. Ordered stdout keys would be ambiguous, so key on the full argv.
AHEAD = {"rev-parse HEAD": "old\n", "rev-parse origin/main": "new\n"}


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


def test_update_merges_origin_main_and_restarts(tmp_path: Path) -> None:
    """Merge, never checkout: self-edit means the install always carries local
    commits, and a tag checkout would throw them out of the working tree."""
    runner = FakeRunner(stdout=AHEAD)
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
    runner = FakeRunner(
        stdout={"rev-parse HEAD": "same\n", "rev-parse origin/main": "same\n"}
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
        == 0
    )
    assert not any("merge" in " ".join(c) for c in runner.calls)
    assert any("already up to date" in line for line in said)


def test_update_refuses_a_dirty_tracked_tree(tmp_path: Path) -> None:
    """Untracked installed skills are normal and must not block; modified
    tracked files would be silently clobbered by -X theirs, so refuse."""
    runner = FakeRunner(stdout={**AHEAD, "status --porcelain": " M src/x.py\n"})
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
    assert not any("merge" in " ".join(c) for c in runner.calls)
    assert any("uncommitted" in line for line in said)


def test_update_aborts_a_conflicted_merge(tmp_path: Path) -> None:
    runner = FakeRunner(stdout=AHEAD, fails={"merge -X theirs": "CONFLICT"})
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
    runner = FakeRunner(stdout=AHEAD)
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
    assert (
        update(
            repo_dir=tmp_path,
            runner=FakeRunner(stdout=AHEAD),
            service=_update_repo(tmp_path),
            say=said.append,
            healthy=lambda: True,
        )
        == 0
    )
    assert (installed / "SKILL.md").read_text() == "new text"
    assert not (tmp_path / "skills" / "maps").exists()
    assert any("imsg" in line for line in said)


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
