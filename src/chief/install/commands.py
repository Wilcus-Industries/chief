"""The ``python -m chief.install`` CLI: parser + dispatch over lifecycle ops."""

import argparse
import os
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from chief.install.lifecycle import (
    DEFAULT_LAUNCHER,
    uninstall,
    wait_for_health,
    web_url,
)
from chief.install.service import ServiceManager
from chief.install.units import default_runner
from chief.install.update import update
from chief.install.wizard import WizardIO, run_wizard

DEFAULT_CONFIG = Path("config.yaml")
DEFAULT_CONFIG_TEMPLATE = Path("config.default.yaml")


def ensure_config(
    config_path: Path = DEFAULT_CONFIG,
    template: Path = DEFAULT_CONFIG_TEMPLATE,
) -> bool:
    """Seed config.yaml from the tracked template on first run.

    config.yaml is install-local state (gitignored): the wizard writes the
    budget cap into it in place, and if it were tracked that edit would leave
    the tree permanently dirty — which the self-edit pipeline refuses to run
    against. Returns True when a fresh copy was made.
    """
    if config_path.exists() or not template.exists():
        return False
    config_path.write_text(template.read_text())
    return True


def _build_parser() -> argparse.ArgumentParser:
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
    update_cmd = sub.add_parser("update", help="merge origin/main and restart")
    update_cmd.add_argument("--repo", type=Path, default=Path.cwd())
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
    return parser


def _dispatch(args: argparse.Namespace) -> int:  # noqa: PLR0911
    command: str = args.command
    if command == "wizard":
        ensure_config()
        result = run_wizard(
            secrets_dir=Path("secrets"),
            config_path=DEFAULT_CONFIG,
            io=WizardIO(),
            interactive=not args.non_interactive,
            env=os.environ,
        )
        print(
            f"wizard done: password {result.password}, model auth "
            f"{result.model_auth}, budget {result.budget}."
        )
        return 0
    if command == "service-install":
        ServiceManager.detect().install(repo_dir=args.repo, launcher=args.launcher)
        print("autostart service installed and started.")
        return 0
    if command == "service-uninstall":
        ServiceManager.detect().uninstall()
        print("autostart service removed.")
        return 0
    if command in ("start", "stop"):
        service = ServiceManager.detect()
        if not service.installed:
            print("no autostart service installed — run install.sh first.")
            return 1
        (service.start if command == "start" else service.stop)()
        print("started." if command == "start" else "stopped.")
        return 0
    if command == "status":
        service = ServiceManager.detect()
        print(f"service: {service.status()}")
        url = web_url(args.port)
        up = wait_for_health(url, timeout=2.0)
        print(f"web:     {url} ({'responding' if up else 'not responding'})")
        return 0
    if command == "update":
        return update(
            repo_dir=args.repo,
            runner=default_runner,
            service=ServiceManager.detect(),
        )
    if command == "uninstall":
        return uninstall(
            service=ServiceManager.detect(),
            launcher=args.launcher,
            repo_dir=args.repo,
            purge_data=args.purge_data,
            assume_yes=args.yes,
        )
    if command == "await-health":
        url = web_url(args.port)
        if wait_for_health(url, timeout=args.timeout):
            print(f"web UI is up: {url}")
            return 0
        print(f"web UI did not answer within {args.timeout:.0f}s: {url}")
        return 1
    if command == "open-browser":
        url = web_url(args.port)
        opened = webbrowser.open(url)
        print(url if opened else f"open {url} yourself (no browser found)")
        return 0
    raise AssertionError(f"unhandled command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one lifecycle command; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
