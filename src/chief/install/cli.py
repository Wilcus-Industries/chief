"""The ``python -m chief.install`` CLI surface: one argparse table.

Kept apart from :mod:`.commands` so the parser reads as the list of commands
chief and its owner can run, and that file reads as what each one does.
"""

import argparse
from pathlib import Path

from chief.install.lifecycle import DEFAULT_LAUNCHER


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chief.install",
        description="chief install + lifecycle commands",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    wizard = sub.add_parser("wizard", help="run the first-run wizard")
    wizard.add_argument("--non-interactive", action="store_true")
    service_install = sub.add_parser(
        "service-install", help="install + start the autostart service"
    )
    service_install.add_argument("--repo", type=Path, default=Path.cwd())
    service_install.add_argument("--launcher", type=Path, default=DEFAULT_LAUNCHER)
    sub.add_parser("service-uninstall", help="remove the autostart service")
    sub.add_parser("start", help="start the daemon via the service")
    sub.add_parser("stop", help="stop the daemon via the service")
    status = sub.add_parser("status", help="service + web UI state")
    status.add_argument("--port", type=int, default=None)
    update_cmd = sub.add_parser(
        "update", help="apply the newest release onto this box's own edits"
    )
    update_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    check = sub.add_parser("check-updates", help="is a newer core release out?")
    check.add_argument("--repo", type=Path, default=Path.cwd())
    release_cmd = sub.add_parser("release", help="cut a release (upstream only)")
    release_cmd.add_argument("part", choices=("major", "minor", "patch"))
    release_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    uninstall_cmd = sub.add_parser("uninstall", help="remove service + launcher")
    uninstall_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    uninstall_cmd.add_argument("--launcher", type=Path, default=DEFAULT_LAUNCHER)
    uninstall_cmd.add_argument("--purge-data", action="store_true")
    uninstall_cmd.add_argument("--yes", action="store_true")
    health = sub.add_parser("await-health", help="wait until the web UI answers")
    health.add_argument("--timeout", type=float, default=120.0)
    health.add_argument("--port", type=int, default=None)
    open_cmd = sub.add_parser("open-browser", help="open the web UI")
    open_cmd.add_argument("--port", type=int, default=None)
    compact_cmd = sub.add_parser(
        "compact", help="force-compact a thread's history via the running daemon"
    )
    compact_cmd.add_argument(
        "thread", help="thread_key to compact (e.g. the iMessage self-chat handle)"
    )
    compact_cmd.add_argument(
        "--socket", default=None, help="daemon socket path (default: from config)"
    )
    return parser
