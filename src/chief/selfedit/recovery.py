"""Boot-side half of the self-edit seatbelt.

The pipeline leaves a marker file before restarting. If the new code boots,
the marker is cleared (healthcheck passed). If boot raises, the repo is
reset to the recorded commit and the daemon re-execs into the old code.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

MARKER_NAME = ".selfedit-pending.json"


def clear_marker(repo_root: Path) -> None:
    """Boot succeeded: the self-edit (if any) is healthy."""
    marker = repo_root / MARKER_NAME
    if marker.exists():
        logger.info("self-edit healthcheck passed: %s", marker.read_text())
        marker.unlink()


def rollback_if_marked(repo_root: Path) -> bool:
    """After a failed boot: reset to the pre-edit commit if one is recorded.

    Returns True when a rollback happened (caller should re-exec).
    """
    marker = repo_root / MARKER_NAME
    if not marker.exists():
        return False
    target = str(json.loads(marker.read_text())["rollback_to"])
    marker.unlink()
    logger.error("boot failed after self-edit; rolling back to %s", target)
    subprocess.run(
        ["git", "reset", "--hard", target], cwd=repo_root, check=True
    )
    return True


def restart_daemon() -> None:
    """Replace this process with a fresh daemon (works under any supervisor)."""
    os.execv(sys.executable, [sys.executable, "-m", "chief.entrypoint"])
