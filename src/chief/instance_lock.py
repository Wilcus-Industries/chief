"""Single-instance guard: an flock the daemon holds for its whole life.

A second ``chief run`` / launchd job pointing at the same data dir fails to
acquire and refuses to start, instead of quietly double-polling ``chat.db`` and
answering every iMessage twice (the double-send). The lock is an advisory
``flock`` on ``<data_dir>/chief.lock``; the OS drops it when the holding process
exits — SIGKILL, crash, or the self-edit execv that replaces the image — so a
dead daemon never wedges the next boot (no stale-PID liveness dance).
"""

import fcntl
from pathlib import Path
from typing import TextIO


class AlreadyRunning(RuntimeError):
    """Raised when another daemon already holds the instance lock."""


def acquire_instance_lock(lock_path: Path) -> TextIO:
    """Take the single-instance flock, returning the held file handle.

    Keep the returned handle alive for the process's whole life — closing it
    (or the process exiting) releases the lock. Raises :class:`AlreadyRunning`
    if another process already holds it.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise AlreadyRunning(f"another chief instance holds {lock_path}") from exc
    return handle
