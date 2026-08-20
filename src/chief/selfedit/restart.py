"""Restart side of the self-edit seatbelt: when the daemon is allowed to go.

A self-edit runs *inside* a turn, so the process may not be replaced the
instant the done-check goes green — the exchange that asked for the edit, and
any turn running beside it, would be lost uncommitted. The pipeline requests a
restart here; the session fires it at the outermost boundary, once replies and
cursors are durable.

The other half of the seatbelt — what happens when the new code fails to boot
— lives in ``chief.selfedit.recovery``.
"""

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from chief.selfedit.notice import RestartNotice, write_restart_notice

logger = logging.getLogger(__name__)

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


def restart_daemon() -> None:
    """Replace this process with a fresh daemon (works under any supervisor)."""
    os.execv(sys.executable, [sys.executable, "-m", "chief.entrypoint"])
