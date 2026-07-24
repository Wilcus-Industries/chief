"""The ``python -m chief.install`` CLI: dispatch over the lifecycle ops.

The argument parser — the CLI's public surface — lives in :mod:`.cli`.
"""

import argparse
import asyncio
import os
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from chief.config import load_config
from chief.install.cli import build_parser
from chief.install.lifecycle import (
    uninstall,
    wait_for_health,
    web_url,
)
from chief.install.release import cut_release
from chief.install.service import ServiceManager
from chief.install.update import abort_update, update
from chief.install.updatecheck import refresh
from chief.install.wizard import WizardIO, run_wizard
from chief.socket_client import send_once

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
        if args.abort:
            return abort_update(repo_dir=args.repo)
        return update(repo_dir=args.repo)
    if command == "release":
        return cut_release(repo_dir=args.repo, part=args.part)
    if command == "check-updates":
        # Synchronous on purpose: the daemon's hook reads the cache this writes,
        # but someone typing the command wants an answer, not yesterday's.
        status = refresh(args.repo)
        if status is None:
            print("could not check: git did not answer (offline? auth?)")
            return 1
        if not status.behind:
            print(f"up to date ({status.latest}).")
            return 0
        running = status.current or "an unrecorded base"
        print(
            f"{status.latest} is out (this box is on {running}) "
            "— run `chief update`."
        )
        return 0
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
    if command == "compact":
        # A one-shot socket client: the compaction runs in the live daemon so
        # the cached in-memory session is folded too (a bare DB rewrite would be
        # clobbered by that session's next commit). The socket forces a `cli:`
        # thread, so the target thread rides as the `/compact` argument.
        socket_path = args.socket or str(load_config().socket_path)
        # Per-pid thread id: the socket adapter keeps only the last writer per
        # thread_key, so two concurrent `chief compact` runs must not collide.
        job_thread = f"compact-job-{os.getpid()}"
        try:
            reply = asyncio.run(
                send_once(socket_path, job_thread, f"/compact {args.thread}")
            )
        except ConnectionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(reply)
        return 0
    raise AssertionError(f"unhandled command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one lifecycle command; returns the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
