"""Boot-side half of the self-edit seatbelt.

The pipeline leaves a marker file before restarting. If the new code boots,
the marker is cleared (healthcheck passed). If boot raises, whatever the
restart changed is undone and the daemon restarts into the old state — the
repo resets to the recorded commit, and the config is put back from its
newest history snapshot, which git cannot do because config.yaml is ignored.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from chief.config.history import restore_newest
from chief.selfedit.notice import RestartNotice, write_restart_notice

logger = logging.getLogger(__name__)

MARKER_NAME = ".selfedit-pending.json"

#: The config a failed boot was rolled back *from*, kept beside the repo so a
#: hand edit is never silently discarded by the recovery.
FAILED_CONFIG_NAME = ".config-failed.yaml"

# How long to let in-flight turns commit before restarting anyway. A stuck
# turn must not wedge the restart forever; a self-edit already merged.
DRAIN_TIMEOUT_SECONDS = 30.0


class RestartBoundary(Protocol):
    """The outermost side-effect boundary a caller fires after a turn's reply
    (and any cursor) is durable, so a pending self-edit execs last. Satisfied
    by ``RestartController``; a no-op elsewhere."""

    async def fire_if_requested(self) -> None: ...


class RestartController:
    """Restarts the daemon only once in-flight turns have committed.

    A self-edit / install runs *inside* a turn. If the pipeline called os.execv
    the instant the check went green, the process would vanish before any turn
    persisted — the exchange that asked for the edit would be lost (the daemon
    reboots amnesiac and re-asks in a loop), and any *other* turn running
    concurrently would be killed uncommitted too. Instead the pipeline calls
    ``request`` mid-turn; the session brackets every turn with ``enter_turn`` /
    ``leave_turn`` and calls ``fire_if_requested`` after it commits. On a pending
    restart, new turns are held at ``enter_turn`` and the restart waits for the
    active turns to drain (bounded by a timeout) before exec'ing, so their
    transcripts reach disk first.
    """

    def __init__(
        self,
        restart: Callable[[], None] | None = None,
        drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
        repo_root: Path = Path("."),
    ) -> None:
        self._restart = restart if restart is not None else restart_daemon
        self._drain_timeout = drain_timeout
        self._repo_root = repo_root
        self._notice: RestartNotice | None = None
        self._requested = False
        self._active = 0
        self._admitting = asyncio.Event()
        self._admitting.set()  # open until a restart is requested
        self._idle = asyncio.Event()
        self._idle.set()  # set whenever no turn is active

    def request(self, notice: RestartNotice | None = None) -> None:
        """Mark a restart due and stop admitting new turns (pipeline side).

        ``notice`` is the thread to report back to; it is held in memory and
        only written at the exec — the request and the exec are a whole turn
        plus the drain apart, and a notice on disk in between would be
        claimed by any unrelated reboot that beat this one to it.
        """
        self._requested = True
        self._notice = notice
        self._admitting.clear()

    async def enter_turn(self) -> None:
        """Admission gate: hold new turns once a restart is pending, then
        register as active so the drain waits for this turn to commit."""
        await self._admitting.wait()
        self._active += 1
        self._idle.clear()

    def leave_turn(self) -> None:
        """Deregister a turn that has finished (and committed)."""
        self._active -= 1
        if self._active == 0:
            self._idle.set()

    async def fire_if_requested(self) -> None:
        """If a restart is pending, wait for other turns to drain, then exec.

        Never returns when it restarts (os.execv). The drain is bounded: a turn
        that outlasts the timeout is left behind rather than blocking forever.
        """
        if not self._requested:
            return
        try:
            await asyncio.wait_for(self._idle.wait(), self._drain_timeout)
        except TimeoutError:
            logger.warning(
                "restart drain timed out after %.0fs; %d turn(s) still in flight",
                self._drain_timeout,
                self._active,
            )
        if self._notice is not None:
            write_restart_notice(self._repo_root, self._notice)
        self._restart()


def clear_marker(repo_root: Path) -> None:
    """Boot succeeded: the self-edit (if any) is healthy."""
    marker = repo_root / MARKER_NAME
    if marker.exists():
        logger.info("self-edit healthcheck passed: %s", marker.read_text())
        marker.unlink()


def rollback_if_marked(
    repo_root: Path,
    config_history: Path | None = None,
    run: Callable[[list[str]], object] | None = None,
) -> bool:
    """After a failed boot: undo whatever the restart changed.

    Two halves, because a restart changes two things the seatbelt covers
    differently. Committed repo files rewind with ``git reset --hard``. The
    config does not — it is gitignored, so git steps straight past it, and a
    config-only restart commits nothing at all, which is why it used to leave
    no marker and so had no boot-failure recovery for anything. Its undo is
    the newest config-history snapshot.

    Returns True only when something was actually undone. A marker with
    nothing left to undo must return False: rebooting into an identical tree
    and an identical config reproduces the same failed boot forever.

    ``config_history`` comes from the caller rather than the marker: the boot
    that writes the marker and the boot that reads it resolve the data dir the
    same way, so recording it would only let the two drift.

    ``run`` is the git runner, injected by tests so they need no real repo.
    """
    marker = repo_root / MARKER_NAME
    if not marker.exists():
        return False
    record = json.loads(marker.read_text())
    marker.unlink()
    undone = False
    # ``committed`` is absent from a marker written by the previous version,
    # and those were only ever written when a commit had been made.
    if record.get("committed", True) and record.get("rollback_to"):
        target = str(record["rollback_to"])
        logger.error("boot failed after self-edit; rolling back to %s", target)
        argv = ["git", "reset", "--hard", target]
        if run is None:
            subprocess.run(argv, cwd=repo_root, check=True)
        else:
            run(argv)
        undone = True
    if config_history is not None:
        restored = restore_newest(
            config_history, repo_root / "config.yaml", repo_root / FAILED_CONFIG_NAME
        )
        if restored is not None:
            logger.error("boot failed; put the config back from %s", restored)
            undone = True
    if not undone:
        logger.error("boot failed, but nothing was left to undo; not restarting")
    return undone


def restart_daemon() -> None:
    """Replace this process with a fresh daemon (works under any supervisor)."""
    os.execv(sys.executable, [sys.executable, "-m", "chief.entrypoint"])
