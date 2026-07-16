"""Service-definition rendering + the shared subprocess runner.

Pure functions so tests can pin the generated launchd plist / systemd unit
content byte-for-byte.
"""

import plistlib
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

LAUNCHD_LABEL = "com.chief.daemon"
SYSTEMD_UNIT_NAME = "chief.service"

#: Runs one command, capturing output, never raising on non-zero exit.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(
            list(argv), capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            list(argv), 127, stdout="", stderr=f"{argv[0]}: command not found"
        )


def default_path_env(launcher: Path) -> str:
    """A PATH that reaches the launcher, uv, and Homebrew from a service."""
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
    return (
        "[Unit]\n"
        "Description=chief — personal AI agent (host-native daemon)\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={launcher} run\n"
        f"WorkingDirectory={repo_dir}\n"
        f'Environment="PATH={path_env}"\n'
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def launchd_plist(*, launcher: Path, repo_dir: Path, path_env: str) -> str:
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(launcher), "run"],
        "WorkingDirectory": str(repo_dir),
        "EnvironmentVariables": {"PATH": path_env},
        "RunAtLoad": True,
        # Restart crashes, but let `chief stop` (bootout) actually stop it.
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(repo_dir / "data" / "chief.log"),
        "StandardErrorPath": str(repo_dir / "data" / "chief.log"),
    }
    return plistlib.dumps(payload, sort_keys=False).decode()
