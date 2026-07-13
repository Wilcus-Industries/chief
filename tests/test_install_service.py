"""Unit tests for the autostart service manager (#154).

The service is a thin per-platform wrapper around the same launcher (`chief run`):
a launchd agent on macOS, a systemd user unit on Linux. These tests exercise the
rendered definitions and the exact command sequences through a fake runner — no
launchctl/systemctl ever runs.
"""

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from chief.install.service import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT_NAME,
    ServiceManager,
    launchd_plist,
    systemd_unit,
)


class FakeRunner:
    """Records every command; per-prefix return codes are configurable."""

    def __init__(self, failures: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.failures = failures or {}
        self.stdout = "active\n"

    def __call__(
        self, argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for prefix, code in self.failures.items():
            if joined.startswith(prefix):
                return subprocess.CompletedProcess(
                    list(argv), code, stdout="", stderr="boom"
                )
        return subprocess.CompletedProcess(
            list(argv), 0, stdout=self.stdout, stderr=""
        )


def _manager(
    platform: str, home: Path, runner: FakeRunner | None = None
) -> tuple[ServiceManager, FakeRunner]:
    runner = runner or FakeRunner()
    return (
        ServiceManager(platform=platform, home=home, runner=runner, uid=501),
        runner,
    )


def test_systemd_unit_wraps_the_launcher() -> None:
    unit = systemd_unit(
        launcher=Path("/home/o/.local/bin/chief"),
        repo_dir=Path("/home/o/.local/share/chief"),
        path_env="/home/o/.local/bin:/usr/bin:/bin",
    )
    assert "ExecStart=/home/o/.local/bin/chief run" in unit
    assert "WorkingDirectory=/home/o/.local/share/chief" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert 'Environment="PATH=/home/o/.local/bin:/usr/bin:/bin"' in unit


def test_launchd_plist_is_valid_and_wraps_the_launcher() -> None:
    rendered = launchd_plist(
        launcher=Path("/Users/o/.local/bin/chief"),
        repo_dir=Path("/Users/o/.local/share/chief"),
        path_env="/opt/homebrew/bin:/usr/bin:/bin",
    )
    plist = plistlib.loads(rendered.encode())
    assert plist["Label"] == LAUNCHD_LABEL
    assert plist["ProgramArguments"] == ["/Users/o/.local/bin/chief", "run"]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["WorkingDirectory"] == "/Users/o/.local/share/chief"
    assert (
        plist["EnvironmentVariables"]["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin"
    )


def test_linux_install_writes_unit_and_enables_now(tmp_path: Path) -> None:
    manager, runner = _manager("linux", tmp_path)

    manager.install(
        repo_dir=tmp_path / "chief", launcher=tmp_path / "bin" / "chief"
    )

    unit_path = tmp_path / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
    assert unit_path.is_file()
    assert f"ExecStart={tmp_path / 'bin' / 'chief'} run" in unit_path.read_text()
    assert ["systemctl", "--user", "daemon-reload"] in runner.calls
    assert [
        "systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME
    ] in runner.calls
    # Reboot survival for a user unit needs lingering; best-effort.
    assert ["loginctl", "enable-linger"] in runner.calls
    assert manager.installed


def test_linux_install_is_idempotent(tmp_path: Path) -> None:
    manager, _runner = _manager("linux", tmp_path)
    repo, launcher = tmp_path / "chief", tmp_path / "bin" / "chief"

    manager.install(repo_dir=repo, launcher=launcher)
    first = manager.unit_path.read_text()
    manager.install(repo_dir=repo, launcher=launcher)

    assert manager.unit_path.read_text() == first
    assert manager.installed


def test_linux_lingering_failure_does_not_fail_install(tmp_path: Path) -> None:
    runner = FakeRunner(failures={"loginctl": 1})
    manager, _ = _manager("linux", tmp_path, runner)

    manager.install(
        repo_dir=tmp_path / "chief", launcher=tmp_path / "bin" / "chief"
    )

    assert manager.installed


def test_darwin_install_bootstraps_the_agent(tmp_path: Path) -> None:
    manager, runner = _manager("darwin", tmp_path)

    manager.install(
        repo_dir=tmp_path / "chief", launcher=tmp_path / "bin" / "chief"
    )

    plist_path = (
        tmp_path / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    )
    assert plist_path.is_file()
    plistlib.loads(plist_path.read_bytes())  # must stay valid XML
    assert ["launchctl", "bootstrap", "gui/501", str(plist_path)] in runner.calls
    assert [
        "launchctl", "kickstart", f"gui/501/{LAUNCHD_LABEL}"
    ] in runner.calls
    assert manager.installed


def test_darwin_reinstall_bootout_first_then_bootstrap(tmp_path: Path) -> None:
    """Re-running install reloads the agent instead of erroring on a dup."""
    manager, runner = _manager("darwin", tmp_path)
    repo, launcher = tmp_path / "chief", tmp_path / "bin" / "chief"

    manager.install(repo_dir=repo, launcher=launcher)
    manager.install(repo_dir=repo, launcher=launcher)

    boots = [c for c in runner.calls if c[:2] == ["launchctl", "bootstrap"]]
    outs = [c for c in runner.calls if c[:2] == ["launchctl", "bootout"]]
    assert len(boots) == 2 and len(outs) == 2


def test_start_stop_per_platform(tmp_path: Path) -> None:
    linux, linux_runner = _manager("linux", tmp_path)
    linux.start()
    linux.stop()
    assert ["systemctl", "--user", "start", SYSTEMD_UNIT_NAME] in linux_runner.calls
    assert ["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME] in linux_runner.calls

    darwin, darwin_runner = _manager("darwin", tmp_path)
    darwin.plist_path.parent.mkdir(parents=True)
    darwin.plist_path.write_text("<plist/>")
    darwin.start()
    darwin.stop()
    assert [
        "launchctl", "kickstart", f"gui/501/{LAUNCHD_LABEL}"
    ] in darwin_runner.calls
    assert [
        "launchctl", "bootout", f"gui/501/{LAUNCHD_LABEL}"
    ] in darwin_runner.calls


def test_status_reports_not_installed_and_active(tmp_path: Path) -> None:
    manager, runner = _manager("linux", tmp_path)
    assert manager.status() == "not installed"

    manager.install(
        repo_dir=tmp_path / "chief", launcher=tmp_path / "bin" / "chief"
    )
    assert manager.status() == "active"
    assert ["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME] in runner.calls


def test_uninstall_removes_the_definition(tmp_path: Path) -> None:
    for platform in ("linux", "darwin"):
        manager, runner = _manager(platform, tmp_path)
        manager.install(
            repo_dir=tmp_path / "chief", launcher=tmp_path / "bin" / "chief"
        )
        assert manager.installed

        manager.uninstall()

        assert not manager.installed
        # A second uninstall is a no-op, not an error (idempotent lifecycle).
        manager.uninstall()


def test_unsupported_platform_raises() -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        ServiceManager(
            platform="win32", home=Path("/"), runner=FakeRunner(), uid=0
        )
