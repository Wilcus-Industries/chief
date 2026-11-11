"""Restart notices for subagents: who to tell, after the reboot, that their
subagent died with the old process (PRD #308).

A restart doesn't wait for subagents and they aren't resumable, so just before
the exec the pool's live runs are written here; the fresh daemon takes the file
once it serves and wakes each parent with its notice, so the parent can decide
whether to respawn. Stamped and dropped past :data:`NOTICES_TTL_SECONDS` like
the restart notice (``chief.selfedit.notice``), so a file an exec never
followed can't be claimed by some later, unrelated boot.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

NOTICES_FILE = "subagent_notices.json"
NOTICES_TTL_SECONDS = 300.0


def write_notices(data_dir: Path, notices: list[dict[str, Any]]) -> None:
    """Record ``{parent_key, parent_channel, text}`` entries, stamped now."""
    if not notices:
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {"written_at": time.time(), "notices": notices}
    (data_dir / NOTICES_FILE).write_text(json.dumps(payload))


def take_notices(data_dir: Path) -> list[dict[str, str]]:
    """Read and consume the file; empty when absent, stale, or unreadable —
    never raises, a corrupt file must not block a boot."""
    path = data_dir / NOTICES_FILE
    if not path.exists():
        return []
    notices: list[dict[str, str]] = []
    try:
        data = json.loads(path.read_text())
        age = time.time() - float(data.get("written_at", 0.0))
        if age > NOTICES_TTL_SECONDS:
            logger.warning("dropping subagent notices written %.0fs ago", age)
        else:
            notices = [
                {k: str(n[k]) for k in ("parent_key", "parent_channel", "text")}
                for n in data.get("notices", [])
            ]
    except (ValueError, KeyError, TypeError):
        logger.exception("unreadable subagent notices at %s", path)
    path.unlink()
    return notices
