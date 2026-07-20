"""Lifecycle basics: health polling and uninstall.

``update`` is big enough to own a module — see :mod:`.update`. The CLI
wrapping these lives in :mod:`.commands`.
"""

import shutil
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from chief.config import load_config
from chief.install.service import ServiceManager

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
