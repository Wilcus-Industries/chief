"""The restart notice: who asked for a restart, so the reboot reports back.

A restart replaces the process mid-conversation, and the owner had no way to
tell when chief was serving again short of poking the thread. The controller
writes the requesting thread here in the breath before ``os.execv``; the fresh
daemon consumes the file once its adapters are up and posts the outcome there.

Two rules keep the report honest, because a notice on disk is claimed by
whatever boots next — not necessarily the restart that wrote it:

* written as late as possible (at the exec, not when the restart is requested
  — those are a whole turn plus the drain apart), so the window in which an
  unrelated crash or ``launchctl kickstart`` could claim it is microseconds;
* stamped, and dropped unread past :data:`NOTICE_TTL_SECONDS` — a notice whose
  exec never happened expires instead of congratulating a later reboot.

Written under gitignored ``data/`` so it never lands in a self-edit commit,
and consumed on read so a later crash-restart can't resurrect a stale
"restart success".
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

NOTICE_PATH = Path("data") / "restart_notice.json"

# A restart's own reboot lands in seconds (the done-check ran before the exec).
# Anything older means the exec never happened and something else is booting.
NOTICE_TTL_SECONDS = 300.0


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
    """Record the requesting thread, stamped now. Call it at the exec."""
    path = repo_root / NOTICE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "channel": notice.channel,
                "thread_key": notice.thread_key,
                "rationale": notice.rationale,
                "rolled_back": notice.rolled_back,
                "written_at": time.time(),
            }
        )
    )


def take_restart_notice(repo_root: Path) -> RestartNotice | None:
    """Read and consume the notice (None when no restart is owed a report).

    Always removes the file: a notice this boot won't report is a notice no
    later boot should report either.
    """
    path = repo_root / NOTICE_PATH
    if not path.exists():
        return None
    notice = None
    try:
        data = json.loads(path.read_text())
        age = time.time() - float(data.get("written_at", 0.0))
        if age > NOTICE_TTL_SECONDS:
            logger.warning("dropping restart notice written %.0fs ago", age)
        else:
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
