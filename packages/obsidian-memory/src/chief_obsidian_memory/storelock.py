"""Cross-process lock over the index home.

The ambient hook (in the daemon) and the ``chief-memory`` CLI open the same
SQLite store from different processes; a CLI ``reindex`` racing the hook's
self-heal would mutate one store concurrently. SQLite's own locking would keep
the file intact but not the *work*: a rebuild interleaved with a sweep can drop
rows one of them still assumes are there. Every mutating or
self-healing entry point takes this advisory flock first — the
``chief.instance_lock`` pattern, but blocking (the loser waits its turn, then
proceeds) and re-entrant within a process (search's self-heal calls build
under the already-held lock).
"""

import fcntl
from pathlib import Path
from types import TracebackType
from typing import TextIO


class StoreLock:
    """A blocking, per-process re-entrant flock on ``<index_home>/.lock``.

    Assumes one holder per process (daemon hook OR separate-process CLI):
    two instances in one process hold distinct descriptors, and flock does
    not nest across those, so they would block each other."""

    def __init__(self, index_home: Path) -> None:
        self._path = Path(index_home) / ".lock"
        self._handle: TextIO | None = None
        self._depth = 0

    def __enter__(self) -> "StoreLock":
        if self._depth == 0:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._path.open("w")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except BaseException:
                handle.close()  # a flock failure must not leak the fd
                raise
            self._handle = handle
        self._depth += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._depth -= 1
        if self._depth == 0 and self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
