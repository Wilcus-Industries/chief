"""Is a newer core release out? A cached verdict chief can act on.

The check never runs inside a turn. ``session_start`` reads a cached answer
off disk — zero network, zero latency — and schedules a refresh in the
background when that answer has gone stale, so the *next* session sees fresh
news. A ``git fetch`` in the turn path would put the network between the owner
and a reply, and an unbounded git call already wedged the whole daemon once.

It speaks in **releases**, not in commits behind a branch: a self-editing
install is permanently ahead of and behind any branch at once, so a commit
count never meant anything. What a box has is a pinned release plus its own
layer, and the only question is whether a newer release exists.
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from chief.hooks.context import TurnContext
from chief.install import basepin, releases

logger = logging.getLogger(__name__)

STATUS_PATH = Path("data/update_status.json")

#: Bounded like every other git call chief makes: a stalled network or an auth
#: prompt must degrade to "no news", never to a hang.
FETCH_TIMEOUT_SECONDS = 20.0

#: How old a cached verdict may be before a background refresh is scheduled.
STALE_AFTER_SECONDS = 6 * 60 * 60

_refreshing = False


@dataclass(frozen=True)
class UpdateStatus:
    """The release this box is pinned at, and the newest one published."""

    current: str
    latest: str
    checked_at: float

    @property
    def stale(self) -> bool:
        return (time.time() - self.checked_at) > STALE_AFTER_SECONDS

    @property
    def behind(self) -> bool:
        return bool(self.latest) and self.latest != self.current


def refresh(
    repo_dir: Path, *, status_path: Path = STATUS_PATH
) -> UpdateStatus | None:
    """Fetch tags and re-resolve, writing the cache. ``None`` if git can't answer."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        fetched = _git(
            ["git", "-C", str(repo_dir), "fetch", "--quiet", "--tags", "origin"], env
        )
    except subprocess.TimeoutExpired:
        logger.warning("update check timed out after %ss", FETCH_TIMEOUT_SECONDS)
        return None
    if fetched.returncode != 0:
        logger.warning("update check fetch failed: %s", fetched.stderr.strip())
        return None
    latest = releases.newest_release(repo_dir)
    if latest is None:
        return None
    status = UpdateStatus(
        current=_pinned_tag(repo_dir),
        latest=latest.tag,
        checked_at=time.time(),
    )
    _write(status, status_path)
    return status


def _pinned_tag(repo_dir: Path) -> str:
    """The tag of the pinned base, or ``""`` when it is not a release commit."""
    base = basepin.read_base(repo_dir)
    if base is None:
        return ""
    for release in releases.all_releases(repo_dir):
        if release.commit == base:
            return release.tag
    return ""


def _git(argv: list[str], env: dict[str, str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
        timeout=FETCH_TIMEOUT_SECONDS,
    )


def _write(status: UpdateStatus, status_path: Path) -> None:
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(
                {
                    "current": status.current,
                    "latest": status.latest,
                    "checked_at": status.checked_at,
                }
            )
        )
    except OSError as exc:  # a read-only or full disk must not break a turn
        logger.warning("could not write %s: %s", status_path, exc)


def read_status(status_path: Path = STATUS_PATH) -> UpdateStatus | None:
    """Load the cached verdict. Missing or corrupt reads as "no news"."""
    try:
        raw = json.loads(status_path.read_text())
        return UpdateStatus(
            current=str(raw["current"]),
            latest=str(raw["latest"]),
            checked_at=float(raw["checked_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def notice(status: UpdateStatus | None) -> str | None:
    """The line chief sees, or ``None`` when there is nothing worth saying."""
    if status is None or not status.behind:
        return None
    running = f"on {status.current}" if status.current else "on an unrecorded base"
    return (
        f"A new core release is out: {status.latest} (you are {running}). "
        "You update yourself — load the `self-update` skill and follow it. "
        "Mention it if it is relevant; otherwise wait for the schedule or for "
        "the owner to ask."
    )


def session_start_notice(
    repo_dir: Path, *, status_path: Path = STATUS_PATH
) -> Callable[[TurnContext], Awaitable[str | None]]:
    """Build the ``session_start`` hook that reports a pending core release.

    Reads only. When the cached verdict has aged out it kicks off a refresh in
    the background and still returns the old answer immediately, so a session
    never waits on git.
    """

    async def hook(turn: TurnContext) -> str | None:
        status = read_status(status_path)
        if status is None or status.stale:
            _schedule_refresh(repo_dir, status_path)
        return notice(status)

    return hook


def _schedule_refresh(repo_dir: Path, status_path: Path) -> None:
    """Run one refresh off the turn path; never more than one at a time."""
    global _refreshing
    if _refreshing:
        return
    _refreshing = True

    async def run() -> None:
        global _refreshing
        try:
            await asyncio.to_thread(refresh, repo_dir, status_path=status_path)
        except Exception:
            logger.exception("background update check failed")
        finally:
            _refreshing = False

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:  # no running loop (CLI, tests) — nothing to schedule
        _refreshing = False
