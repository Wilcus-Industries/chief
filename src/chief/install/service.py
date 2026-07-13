"""The autostart service (#154): launchd agent on macOS, systemd user unit on Linux.

Both definitions are thin wrappers around the same installed launcher
(``chief run``), rendered by pure functions so tests can pin their content. All
launchctl/systemctl process work goes through one injected ``runner`` so the
manager is unit-testable — and so the shell scripts stay logic-free.

Platform notes baked in here:

- **systemd**: a *user* unit only survives logout/reboot when lingering is on, so
  install attempts ``loginctl enable-linger`` (best-effort — polkit may refuse in
  odd setups; the unit still autostarts at login).
- **launchd**: ``KeepAlive.SuccessfulExit=false`` restarts crashes but lets
  ``chief stop`` (``launchctl bootout``) actually stop the daemon; ``RunAtLoad``
  brings it back at the next login/boot while the plist stays installed. Agents
  get a minimal PATH from launchd, so the plist pins one that reaches ``uv``.
"""

import logging
import os
import plistlib
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

LAUNCHD_LABEL = "com.chief.daemon"
SYSTEMD_UNIT_NAME = "chief.service"

#: Runs one command, capturing output, never raising on non-zero exit.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False
    )


def default_path_env(launcher: Path) -> str:
    """A PATH that reaches the launcher, uv, and Homebrew from a service context."""
    candidates = [
        str(launcher.parent),
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    seen: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)
    return ":".join(seen)


def systemd_unit(*, launcher: Path, repo_dir: Path, path_env: str) -> str:
    """Render the systemd user unit wrapping ``<launcher> run``."""
    return (
        "[Unit]\n"
        "Description=chief — personal AI agent (host-native daemon)\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={launcher} run\n"
        f"WorkingDirectory={repo_dir}\n"
        f'Environment="PATH={path_env}"\n'
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def launchd_plist(*, launcher: Path, repo_dir: Path, path_env: str) -> str:
    """Render the launchd agent plist wrapping ``<launcher> run``."""
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(launcher), "run"],
        "WorkingDirectory": str(repo_dir),
        "EnvironmentVariables": {"PATH": path_env},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(repo_dir / "data" / "chief.log"),
        "StandardErrorPath": str(repo_dir / "data" / "chief.log"),
    }
    return plistlib.dumps(payload, sort_keys=False).decode()


@dataclass
class ServiceManager:
    """Install/uninstall/start/stop/status for the per-platform autostart unit."""

    platform: str
    home: Path
    runner: Runner = _default_runner
    uid: int = field(default_factory=os.getuid)

    def __post_init__(self) -> None:
        if self.platform not in ("darwin", "linux"):
            raise ValueError(f"unsupported platform: {self.platform!r}")

    @classmethod
    def detect(cls, runner: Runner = _default_runner) -> "ServiceManager":
        """The manager for this machine (``sys.platform`` normalized)."""
        platform = "darwin" if sys.platform == "darwin" else "linux"
        return cls(
            platform=platform, home=Path.home(), runner=runner, uid=os.getuid()
        )

    # ---- paths -----------------------------------------------------------

    @property
    def unit_path(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    @property
    def definition_path(self) -> Path:
        return self.plist_path if self.platform == "darwin" else self.unit_path

    @property
    def installed(self) -> bool:
        return self.definition_path.is_file()

    @property
    def _domain_target(self) -> str:
        return f"gui/{self.uid}/{LAUNCHD_LABEL}"

    # ---- lifecycle ---------------------------------------------------------

    def install(self, *, repo_dir: Path, launcher: Path) -> None:
        """Write the definition and enable + start it; idempotent on re-runs."""
        path_env = default_path_env(launcher)
        self.definition_path.parent.mkdir(parents=True, exist_ok=True)
        if self.platform == "darwin":
            # Reload cleanly on re-install: bootstrap of an already-loaded label
            # errors, so bootout first (a failure there just means "not loaded").
            self.runner(["launchctl", "bootout", self._domain_target])
            self.definition_path.write_text(
                launchd_plist(
                    launcher=launcher, repo_dir=repo_dir, path_env=path_env
                )
            )
            self._checked(
                ["launchctl", "bootstrap", f"gui/{self.uid}",
                 str(self.plist_path)]
            )
            self.runner(["launchctl", "kickstart", self._domain_target])
        else:
            self.definition_path.write_text(
                systemd_unit(
                    launcher=launcher, repo_dir=repo_dir, path_env=path_env
                )
            )
            self._checked(["systemctl", "--user", "daemon-reload"])
            self._checked(
                ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME]
            )
            linger = self.runner(["loginctl", "enable-linger"])
            if linger.returncode != 0:
                logger.warning(
                    "loginctl enable-linger failed (%s) — chief will autostart "
                    "at login, but not before you log in after a reboot",
                    linger.stderr.strip(),
                )

    def uninstall(self) -> None:
        """Stop and remove the autostart unit; a no-op when not installed."""
        if self.platform == "darwin":
            self.runner(["launchctl", "bootout", self._domain_target])
        elif self.installed:
            self.runner(
                ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME]
            )
        self.definition_path.unlink(missing_ok=True)
        if self.platform == "linux":
            self.runner(["systemctl", "--user", "daemon-reload"])

    def start(self) -> None:
        if self.platform == "darwin":
            # Bootstrap covers the post-`chief stop` state (bootout unloads the
            # agent); "already bootstrapped" is fine, kickstart does the start.
            self.runner(
                ["launchctl", "bootstrap", f"gui/{self.uid}",
                 str(self.plist_path)]
            )
            self._checked(["launchctl", "kickstart", self._domain_target])
        else:
            self._checked(["systemctl", "--user", "start", SYSTEMD_UNIT_NAME])

    def stop(self) -> None:
        if self.platform == "darwin":
            self._checked(["launchctl", "bootout", self._domain_target])
        else:
            self._checked(["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME])

    def status(self) -> str:
        """A one-word service state (``not installed`` when no definition)."""
        if not self.installed:
            return "not installed"
        if self.platform == "darwin":
            result = self.runner(["launchctl", "print", self._domain_target])
            if result.returncode != 0:
                return "stopped"
            return "running" if "state = running" in result.stdout else "loaded"
        result = self.runner(
            ["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME]
        )
        return result.stdout.strip() or "unknown"

    def _checked(self, argv: list[str]) -> None:
        result = self.runner(argv)
        if result.returncode != 0:
            raise RuntimeError(
                f"{' '.join(argv)} failed ({result.returncode}): "
                f"{result.stderr.strip()}"
            )
