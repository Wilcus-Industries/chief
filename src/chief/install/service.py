"""The autostart service: launchd agent on macOS, systemd user unit on Linux.

Both wrap the installed launcher (``chief run``); definitions come from
:mod:`.units` and all launchctl/systemctl work goes through one injected
``runner``, so everything here tests without touching the machine. systemd
user units need lingering to survive reboot (attempted best-effort); launchd
agents get a minimal PATH, so the plist pins one that reaches ``uv``.
"""

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from chief.install.units import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT_NAME,
    Runner,
    default_path_env,
    default_runner,
    launchd_plist,
    systemd_unit,
)

logger = logging.getLogger(__name__)


@dataclass
class ServiceManager:
    """Install/uninstall/start/stop/status for the autostart unit."""

    platform: str
    home: Path
    runner: Runner = default_runner
    uid: int = field(default_factory=os.getuid)

    def __post_init__(self) -> None:
        if self.platform not in ("darwin", "linux"):
            raise ValueError(f"unsupported platform: {self.platform!r}")

    @classmethod
    def detect(cls, runner: Runner = default_runner) -> "ServiceManager":
        platform = "darwin" if sys.platform == "darwin" else "linux"
        return cls(
            platform=platform, home=Path.home(), runner=runner, uid=os.getuid()
        )

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

    def install(self, *, repo_dir: Path, launcher: Path) -> None:
        """Write the definition and enable + start it; idempotent."""
        path_env = default_path_env(launcher)
        self.definition_path.parent.mkdir(parents=True, exist_ok=True)
        if self.platform == "darwin":
            # Bootstrap of an already-loaded label errors: bootout first
            # (a failure there just means "not loaded").
            self.runner(["launchctl", "bootout", self._domain_target])
            self.definition_path.write_text(
                launchd_plist(
                    launcher=launcher, repo_dir=repo_dir, path_env=path_env
                )
            )
            self._checked(
                ["launchctl", "bootstrap", f"gui/{self.uid}", str(self.plist_path)]
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
                    "loginctl enable-linger failed (%s) — chief autostarts at "
                    "login, but not before you log in after a reboot",
                    linger.stderr.strip(),
                )

    def uninstall(self) -> None:
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
            # Bootstrap covers the post-`chief stop` state; kickstart starts.
            self.runner(
                ["launchctl", "bootstrap", f"gui/{self.uid}", str(self.plist_path)]
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
        if not self.installed:
            return "not installed"
        if self.platform == "darwin":
            result = self.runner(["launchctl", "print", self._domain_target])
            if result.returncode != 0:
                return "stopped"
            return "running" if "state = running" in result.stdout else "loaded"
        result = self.runner(["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME])
        return result.stdout.strip() or "unknown"

    def _checked(self, argv: list[str]) -> None:
        result = self.runner(argv)
        if result.returncode != 0:
            raise RuntimeError(
                f"{' '.join(argv)} failed ({result.returncode}): "
                f"{result.stderr.strip()}"
            )
