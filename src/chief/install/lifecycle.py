"""The install/lifecycle CLI (#154): ``python -m chief.install <command>``.

The installed ``chief`` launcher dispatches its verbs here (``start``, ``stop``,
``status``, ``update``, ``uninstall``, ``wizard``); ``install.sh`` calls the
plumbing commands (``migrate``, ``service-install``, ``await-health``,
``open-browser``). Keeping every verb in Python keeps the shell scripts logic-free
and this behavior unit-testable.

``update`` pins to the newest **tagged release** — never a branch tip — so
main-branch breakage can't reach an installed owner; migrations run in a fresh
interpreter (the new tree's code), and the autostart service is restarted.
"""

import argparse
import os
import shutil
import sys
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import httpx
import yaml

from ..persistence.db import _run_migrations
from .service import Runner, ServiceManager, _default_runner
from .wizard import WizardIO, run_wizard

#: Where install.sh puts the launcher; the uninstall default.
DEFAULT_LAUNCHER = Path.home() / ".local" / "bin" / "chief"


def resolve_db_path(
    env: Mapping[str, str], config_path: Path = Path("config.yaml")
) -> str:
    """The migration target: ``DB_PATH`` env, else config.yaml, else the default."""
    from_env = env.get("DB_PATH", "")
    if from_env:
        return os.path.expanduser(from_env)
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text())
        if isinstance(loaded, dict):
            configured = loaded.get("db_path")
            if isinstance(configured, str) and configured:
                return os.path.expanduser(configured)
    return "data/chief.db"


def wait_for_health(url: str, *, timeout: float, interval: float = 0.25) -> bool:
    """Poll ``url`` until any HTTP answer (< 500) arrives or ``timeout`` passes.

    Any served response counts as healthy — an unauthenticated hit on the web UI
    redirects to /login or /setup, and either proves the daemon's listener is up.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                url,
                timeout=min(2.0, max(0.1, deadline - time.monotonic())),
                follow_redirects=False,
            )
            if response.status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(max(0.0, min(interval, deadline - time.monotonic())))
    return False


def update(
    *,
    repo_dir: Path,
    runner: Runner,
    service: ServiceManager,
    say: Callable[[str], None] = print,
) -> int:
    """Jump to the newest tagged release: fetch, checkout, sync, migrate, restart."""
    git = ["git", "-C", str(repo_dir)]
    fetch = runner([*git, "fetch", "--tags", "--force", "origin"])
    if fetch.returncode != 0:
        say(f"git fetch failed: {fetch.stderr.strip()}")
        return 1
    tags = runner([*git, "tag", "--sort=-v:refname"]).stdout.split()
    if not tags:
        say(
            "no release tags found — releases are git tags, and none exist "
            "yet. Nothing to update to."
        )
        return 1
    latest = tags[0]
    described = runner([*git, "describe", "--tags", "--exact-match", "HEAD"])
    current = described.stdout.strip() if described.returncode == 0 else ""
    if current == latest:
        say(f"already up to date ({latest}).")
        return 0
    checkout = runner([*git, "checkout", latest])
    if checkout.returncode != 0:
        say(
            f"could not check out {latest} — local changes in {repo_dir} are "
            f"in the way; commit or stash them, then re-run `chief update`. "
            f"({checkout.stderr.strip()})"
        )
        return 1
    steps: tuple[list[str], ...] = (
        [*git, "submodule", "update", "--init", "--recursive"],
        ["uv", "sync"],
        # A fresh interpreter, so the migrations that run are the NEW tree's.
        ["uv", "run", "python", "-m", "chief.install", "migrate"],
    )
    for argv in steps:
        result = runner(argv)
        if result.returncode != 0:
            say(f"{' '.join(argv)} failed: {result.stderr.strip()}")
            return 1
    if service.installed:
        service.stop()
        service.start()
        say("autostart service restarted.")
    else:
        say("no autostart service installed — restart chief yourself.")
    say(f"updated {current or 'an untagged checkout'} → {latest}.")
    return 0


def uninstall(
    *,
    service: ServiceManager,
    launcher: Path,
    repo_dir: Path,
    secrets_dir: Path,
    purge_data: bool,
    assume_yes: bool,
    confirm: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> int:
    """Remove the service and launcher; ``purge_data`` also deletes data+secrets."""
    data_dir = repo_dir / "data"
    if purge_data and not assume_yes:
        answer = confirm(
            f"Delete ALL chief data ({data_dir}) and secrets "
            f"({secrets_dir})? This cannot be undone. [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            say("aborted — nothing removed.")
            return 1
    if service.installed:
        service.uninstall()
        say("autostart service removed.")
    if launcher.exists():
        launcher.unlink()
        say(f"launcher removed: {launcher}")
    if purge_data:
        for target in (data_dir, secrets_dir):
            if target.exists():
                shutil.rmtree(target)
                say(f"removed {target}")
    say(
        f"done. The repo clone remains at {repo_dir} — delete the directory "
        "yourself when you are sure."
    )
    return 0


def open_browser(
    url: str, opener: Callable[[str], bool] = webbrowser.open
) -> int:
    """Open the web UI (best-effort; headless environments just get the URL)."""
    opened = opener(url)
    suffix = "" if opened else " — open it in your browser"
    print(f"web UI: {url}{suffix}")
    return 0


def _web_port(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    # Deferred: chief.app pulls the whole dependency graph, which plumbing
    # commands that were handed an explicit --port never need.
    from ..app import load_settings

    return load_settings().web_port


def _web_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def _cmd_wizard(non_interactive: bool) -> int:
    from ..app import web_auth_dir

    interactive = sys.stdin.isatty() and not non_interactive
    result = run_wizard(
        secrets_dir=Path(web_auth_dir()), io=WizardIO(), interactive=interactive
    )
    print(
        f"wizard done — password: {result.password}, "
        f"model auth: {result.model_auth}"
    )
    return 0


def _cmd_migrate() -> int:
    db_path = resolve_db_path(env=os.environ)
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _run_migrations(db_path)
    print(f"migrations applied: {db_path}")
    return 0


def _cmd_status(port: int | None) -> int:
    print(f"service: {ServiceManager.detect().status()}")
    url = _web_url(_web_port(port))
    responding = wait_for_health(url, timeout=2.0)
    print(f"web:     {url} ({'responding' if responding else 'not responding'})")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chief.install",
        description="chief install + lifecycle commands (#154)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    wizard = sub.add_parser("wizard", help="run the first-run wizard")
    wizard.add_argument("--non-interactive", action="store_true")

    sub.add_parser("migrate", help="apply database migrations")

    service_install = sub.add_parser(
        "service-install", help="install + start the autostart service"
    )
    service_install.add_argument("--repo", type=Path, default=Path.cwd())
    service_install.add_argument(
        "--launcher", type=Path, default=DEFAULT_LAUNCHER
    )
    sub.add_parser("service-uninstall", help="remove the autostart service")

    sub.add_parser("start", help="start the daemon via the service")
    sub.add_parser("stop", help="stop the daemon via the service")
    status = sub.add_parser("status", help="service + web UI state")
    status.add_argument("--port", type=int, default=None)

    update_cmd = sub.add_parser(
        "update", help="jump to the newest tagged release"
    )
    update_cmd.add_argument("--repo", type=Path, default=Path.cwd())

    uninstall_cmd = sub.add_parser(
        "uninstall", help="remove the service + launcher (data kept by default)"
    )
    uninstall_cmd.add_argument("--repo", type=Path, default=Path.cwd())
    uninstall_cmd.add_argument(
        "--launcher", type=Path, default=DEFAULT_LAUNCHER
    )
    uninstall_cmd.add_argument("--purge-data", action="store_true")
    uninstall_cmd.add_argument("--yes", action="store_true")

    await_health = sub.add_parser(
        "await-health", help="wait until the web UI answers"
    )
    await_health.add_argument("--timeout", type=float, default=120.0)
    await_health.add_argument("--port", type=int, default=None)

    open_cmd = sub.add_parser("open-browser", help="open the web UI")
    open_cmd.add_argument("--port", type=int, default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one lifecycle command; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    command: str = args.command
    try:
        if command == "wizard":
            return _cmd_wizard(args.non_interactive)
        if command == "migrate":
            return _cmd_migrate()
        if command == "service-install":
            ServiceManager.detect().install(
                repo_dir=args.repo, launcher=args.launcher
            )
            print("autostart service installed and started.")
            return 0
        if command == "service-uninstall":
            ServiceManager.detect().uninstall()
            print("autostart service removed.")
            return 0
        if command == "start":
            service = ServiceManager.detect()
            if not service.installed:
                print(
                    "no autostart service installed — run install.sh, or "
                    "`chief run` for a foreground daemon."
                )
                return 1
            service.start()
            print("started.")
            return 0
        if command == "stop":
            service = ServiceManager.detect()
            if not service.installed:
                print("no autostart service installed — nothing to stop.")
                return 1
            service.stop()
            print("stopped.")
            return 0
        if command == "status":
            return _cmd_status(args.port)
        if command == "update":
            return update(
                repo_dir=args.repo,
                runner=_default_runner,
                service=ServiceManager.detect(),
            )
        if command == "uninstall":
            from ..app import web_auth_dir

            return uninstall(
                service=ServiceManager.detect(),
                launcher=args.launcher,
                repo_dir=args.repo,
                secrets_dir=Path(web_auth_dir()),
                purge_data=args.purge_data,
                assume_yes=args.yes,
            )
        if command == "await-health":
            url = _web_url(_web_port(args.port))
            if wait_for_health(url, timeout=args.timeout):
                print(f"web UI is up: {url}")
                return 0
            print(f"web UI did not answer within {args.timeout:.0f}s: {url}")
            return 1
        if command == "open-browser":
            return open_browser(_web_url(_web_port(args.port)))
    except RuntimeError as exc:
        # ServiceManager's checked calls raise with the failing command inline.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command: {command}")
