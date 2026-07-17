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
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

MARKER_NAME = ".selfedit-pending.json"


class RestartController:
    """Defers a self-edit restart until the running turn has committed.

    The self-edit pipeline runs *inside* a turn. If it called os.execv the
    instant the check went green, the process would vanish before the session
    persisted the turn — so the very exchange that asked for the edit (an
    install Q&A, say) would be lost, and the daemon would reboot with no memory
    of it and re-ask the same questions in a loop. Instead the pipeline calls
    ``request`` mid-turn and the session calls ``fire_if_requested`` right after
    it commits, so the transcript is on disk before the restart.
    """

    def __init__(self, restart: Callable[[], None] | None = None) -> None:
        self._restart = restart if restart is not None else restart_daemon
        self._requested = False

    def request(self) -> None:
        """Mark a restart due once the current turn commits (pipeline side)."""
        self._requested = True

    def fire_if_requested(self) -> None:
        """Restart if one was requested (os.execv — does not return)."""
        if self._requested:
            self._restart()


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
