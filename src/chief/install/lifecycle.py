"""Lifecycle operations: health polling, release updates, uninstall.

``update`` pins to the newest tagged release — never a branch tip; there are
no migrations to run (the schema is created at boot by the new tree's code).
The CLI wrapping these lives in :mod:`.commands`.
"""

import shutil
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from chief.config import load_config
from chief.install.service import ServiceManager
from chief.install.units import Runner

DEFAULT_LAUNCHER = Path.home() / ".local" / "bin" / "chief"


def web_url(port: int | None = None) -> str:
    config = load_config()
    return f"http://127.0.0.1:{port or config.web_port}/"


def wait_for_health(url: str, *, timeout: float, interval: float = 0.25) -> bool:
    """Poll until any served answer (< 500) arrives — a login redirect counts."""
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
    """Jump to the newest tagged release: fetch, checkout, sync, restart."""
    git = ["git", "-C", str(repo_dir)]
    fetch = runner([*git, "fetch", "--tags", "--force", "origin"])
    if fetch.returncode != 0:
        say(f"git fetch failed: {fetch.stderr.strip()}")
        return 1
    tags = runner([*git, "tag", "--sort=-v:refname"]).stdout.split()
    if not tags:
        say("no release tags found — nothing to update to.")
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
            f"in the way. ({checkout.stderr.strip()})"
        )
        return 1
    sync = runner(["uv", "sync"])
    if sync.returncode != 0:
        say(f"uv sync failed: {sync.stderr.strip()}")
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
    purge_data: bool,
    assume_yes: bool,
    confirm: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> int:
    """Remove service + launcher; ``purge_data`` also deletes data/secrets."""
    if purge_data and not assume_yes:
        answer = confirm(
            f"Delete {repo_dir / 'data'} and {repo_dir / 'secrets'} too? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            say("aborted — nothing removed.")
            return 1
    service.uninstall()
    launcher.unlink(missing_ok=True)
    say("service + launcher removed.")
    if purge_data:
        shutil.rmtree(repo_dir / "data", ignore_errors=True)
        shutil.rmtree(repo_dir / "secrets", ignore_errors=True)
        say("data + secrets removed.")
    else:
        say("data + secrets kept (pass --purge-data to remove them).")
    return 0
