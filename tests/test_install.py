"""Installer plumbing: wizard steps, service rendering, update flow."""

import subprocess
from collections.abc import Sequence
from pathlib import Path

from chief.install.lifecycle import uninstall, update
from chief.install.service import ServiceManager
from chief.install.units import default_path_env, launchd_plist, systemd_unit
from chief.install.wizard import WizardIO, run_wizard

CONFIG = "budget:\n  cap_usd: 0\n  warn_ratio: 0.8\n"


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
    """Records argv calls; scripted stdout per command head."""

    def __init__(self, stdout: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._stdout = stdout or {}

    def __call__(self, argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        for key, out in self._stdout.items():
            if key in " ".join(argv):
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


def test_update_checks_out_latest_tag_and_restarts(tmp_path: Path) -> None:
    runner = FakeRunner(
        stdout={"tag --sort": "v0.3.0\nv0.2.0\n", "describe": "v0.2.0"}
    )
    manager = ServiceManager(
        platform="linux", home=tmp_path, runner=runner, uid=1000
    )
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text("unit")
    said: list[str] = []
    assert (
        update(
            repo_dir=tmp_path, runner=runner, service=manager, say=said.append
        )
        == 0
    )
    joined = [" ".join(c) for c in runner.calls]
    assert any("checkout v0.3.0" in c for c in joined)
    assert any("uv sync" in c for c in joined)
    assert not any("migrate" in c for c in joined)
    assert "autostart service restarted." in said


def test_update_with_no_tags_fails_cleanly(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = ServiceManager(
        platform="linux", home=tmp_path, runner=runner, uid=1000
    )
    said: list[str] = []
    assert (
        update(
            repo_dir=tmp_path, runner=runner, service=manager, say=said.append
        )
        == 1
    )
    assert any("no release tags" in line for line in said)


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
