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
from chief.install.account import DEFAULT_GROUP
from chief.install.account_steps import remove_steps
from chief.install.dedicated import StepRunner, default_step_runner
from chief.install.posture import ACCOUNT_REPORT, chief_account
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
    remove_account: bool = False,
    keep_account: bool = False,
    group: str = DEFAULT_GROUP,
    confirm: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    execute: StepRunner = default_step_runner,
) -> int:
    """Remove service + launcher; ``purge_data`` also deletes data/secrets.

    The dedicated system account is only removed when asked for — its home
    holds chief's own message store. ``--remove-account`` / ``--keep-account``
    are the non-interactive answers; without either, uninstall asks. A
    single-user install has no such account, and is never asked.
    """
    if purge_data and not assume_yes:
        answer = confirm(
            f"Delete {repo_dir / 'data'} and {repo_dir / 'secrets'} too? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            say("aborted — nothing removed.")
            return 1
    # Read before the purge: --purge-data deletes data/, which is where the
    # report lives, and a run told to remove the account would then find no
    # record of one and report that it never existed.
    account = chief_account(repo_dir / ACCOUNT_REPORT)
    service.uninstall()
    launcher.unlink(missing_ok=True)
    say("service + launcher removed.")
    if purge_data:
        shutil.rmtree(repo_dir / "data", ignore_errors=True)
        shutil.rmtree(repo_dir / "secrets", ignore_errors=True)
        say("data + secrets removed.")
    else:
        say("data + secrets kept (pass --purge-data to remove them).")
    # Only this install's own report names the account it set chief up with; a
    # bare `chief` in passwd may be someone else's, and userdel --remove takes
    # the home with it. No report, no question and no steps.
    if account is None:
        say("no dedicated system account is recorded for this install.")
    elif keep_account or not _account_wanted(
        remove_account, assume_yes, account[0], confirm
    ):
        say(
            "system account kept "
            f"(pass --remove-account to delete {account[0]})."
        )
    else:
        for step in remove_steps(service.platform, account[0], group):
            say(f"  {step.description}")
            if execute(step).returncode != 0:
                # Stop: groupdel --force after a failed userdel takes the group
                # out from under an account that is still there.
                say(f"system account {account[0]} could not be removed.")
                return 1
        say(f"system account {account[0]} removed.")
    return 0


def _account_wanted(
    remove_account: bool,
    assume_yes: bool,
    user: str,
    confirm: Callable[[str], str],
) -> bool:
    if remove_account:
        return True
    if assume_yes:
        return False
    answer = confirm(
        f"Delete the {user} system account, its home and its message store "
        "too? [y/N] "
    )
    return answer.strip().lower() in ("y", "yes")
