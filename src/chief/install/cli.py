"""The ``python -m chief.install`` CLI surface: one argparse table.

Kept apart from :mod:`.commands` so the parser reads as the list of commands
chief and its owner can run, and that file reads as what each one does.
"""

import argparse
from pathlib import Path

from chief.install.lifecycle import DEFAULT_LAUNCHER
from chief.install.posture import ACCOUNT_REPORT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chief.install",
        description="chief install + lifecycle commands",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    wizard = sub.add_parser("wizard", help="run the first-run wizard")
    wizard.add_argument("--non-interactive", action="store_true")
    account = sub.add_parser(
        "account", help="give chief its own system user (interactive only)"
    )
    account.add_argument("--non-interactive", action="store_true")
    account.add_argument("--tree", type=Path, default=None)
    account.add_argument(
        # Defaulted, not None: `chief account` is the documented alternative to
        # re-running install.sh (docs/OPERATIONS.md), and uninstall reads this
        # file to decide there is an account at all. Written nowhere, the
        # account it just made is one chief can never remove.
        "--report",
        type=Path,
        default=ACCOUNT_REPORT,
        help="write key=value facts here",
    )
    service_install = sub.add_parser(
        "service-install", help="install + start the autostart service"
    )
    service_install.add_argument("--repo", type=Path, default=Path.cwd())
    service_install.add_argument("--launcher", type=Path, default=DEFAULT_LAUNCHER)
    service_install.add_argument(
        "--home", type=Path, default=None, help="whose home the definition goes in"
    )
    service_install.add_argument("--uid", type=int, default=None)
    service_install.add_argument(
        "--no-start",
        action="store_true",
        help="write the definition only — it loads at that account's next login",
    )
    sub.add_parser("service-uninstall", help="remove the autostart service")
    sub.add_parser("start", help="start the daemon via the service")
    sub.add_parser("stop", help="stop the daemon via the service")
    status = sub.add_parser("status", help="service + web UI state")
    status.add_argument("--port", type=int, default=None)
    update_cmd = sub.add_parser(
        "update", help="apply the newest release onto this box's own edits"
    )
    update_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    update_cmd.add_argument(
        "--abort",
        action="store_true",
        help="give up on an applied update: undo it and forget it",
    )
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
    uninstall_cmd.add_argument(
        "--remove-account",
        action="store_true",
        help="also delete chief's system user, its home and the shared group",
    )
    uninstall_cmd.add_argument(
        "--keep-account",
        action="store_true",
        help="keep chief's system user without being asked",
    )
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
