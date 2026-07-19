"""Cross-process lock over a chroma index home.

The ambient hook (in the daemon) and the ``chief-memory`` CLI open the same
persistent chromadb store from different processes; a CLI ``reindex`` racing
the hook's self-heal would mutate one store concurrently. Every mutating or
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
    """A blocking, per-process re-entrant flock on ``<index_home>/.lock``."""

    def __init__(self, index_home: Path) -> None:
        self._path = Path(index_home) / ".lock"
        self._handle: TextIO | None = None
        self._depth = 0

    def __enter__(self) -> "StoreLock":
        if self._depth == 0:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self._path.open("w")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
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
