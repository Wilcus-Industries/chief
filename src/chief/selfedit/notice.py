"""The restart notice: who asked for a restart, so the reboot reports back.

A restart replaces the process mid-conversation, and the owner had no way to
tell when chief was serving again short of poking the thread. The pipeline
records the requesting thread here just before the execv; the fresh daemon
consumes the file once its adapters are up and posts the outcome there.

Written under gitignored ``data/`` so it never lands in a self-edit commit,
and consumed on read so a later crash-restart can't resurrect a stale
"restart success".
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from chief.agent.tools import ToolContext

logger = logging.getLogger(__name__)

NOTICE_PATH = Path("data") / "restart_notice.json"


@dataclass(frozen=True)
class RestartNotice:
    """Who asked for the restart, and how it turned out."""

    channel: str
    thread_key: str
    rationale: str = ""
    rolled_back: bool = False

    def text(self) -> str:
        """What the fresh daemon says on the requesting thread."""
        if self.rolled_back:
            return (
                "⚠️ restart failed — the new code did not boot, so it was "
                "rolled back. Back up on the previous commit."
            )
        detail = f" ({self.rationale})" if self.rationale else ""
        return f"✅ restart success — back up{detail}"


def write_restart_notice(repo_root: Path, notice: RestartNotice) -> None:
    """Record the requesting thread just before the process is replaced."""
    path = repo_root / NOTICE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "channel": notice.channel,
                "thread_key": notice.thread_key,
                "rationale": notice.rationale,
                "rolled_back": notice.rolled_back,
            }
        )
    )


def record_restart_origin(
    repo_root: Path, origin: ToolContext | None, rationale: str
) -> None:
    """Note the thread a restart was requested from (no-op without one)."""
    if origin is None:
        return
    write_restart_notice(
        repo_root, RestartNotice(origin.channel, origin.thread_key, rationale)
    )


def take_restart_notice(repo_root: Path) -> RestartNotice | None:
    """Read and consume the notice (None when no restart was requested)."""
    path = repo_root / NOTICE_PATH
    if not path.exists():
        return None
    notice = None
    try:
        data = json.loads(path.read_text())
        notice = RestartNotice(
            channel=str(data["channel"]),
            thread_key=str(data["thread_key"]),
            rationale=str(data.get("rationale", "")),
            rolled_back=bool(data.get("rolled_back", False)),
        )
    except Exception:
        # A corrupt notice is dropped, not raised: it must never block a boot.
        logger.exception("unreadable restart notice at %s", path)
    path.unlink()
    return notice


def mark_notice_rolled_back(repo_root: Path) -> None:
    """A failed boot: the pending notice now reports the rollback instead."""
    notice = take_restart_notice(repo_root)
    if notice is None:
        return
    write_restart_notice(
        repo_root,
        RestartNotice(
            channel=notice.channel,
            thread_key=notice.thread_key,
            rationale=notice.rationale,
            rolled_back=True,
        ),
    )
