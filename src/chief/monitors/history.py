"""Thread history for monitor wakes: what was said in a watched thread.

A wake used to carry one event's payload, so a woken session saw only the
messages that fired: never chief's own replies, nothing a judge let pass, and
nothing that arrived while chief was down. A channel that keeps its own record
(iMessage's ``chat.db``) now supplies the thread's recent lines through a
:class:`HistorySource`, and :class:`SeenCursors` remembers, per monitor, the
newest line its last wake showed in each thread, so "new since you last
looked" survives a restart.
"""

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Line:
    """One message in a thread. ``id`` orders lines within their channel's
    store; ``sender`` is the raw handle; ``mine`` marks chief's own sends.
    ``shared`` marks a send from an account chief shares with the owner, which
    could be either of them. ``owner_photos``: the owner attached photos, which
    a wake stashes into ``images`` (paths) only when it shows the line."""

    id: int
    at: float
    sender: str
    text: str
    mine: bool = False
    images: tuple[str, ...] = ()
    owner_photos: bool = False
    shared: bool = False


class HistorySource(Protocol):
    """A channel's own record of its threads."""

    def recent(
        self, thread_key: str, limit: int, store: str = ""
    ) -> tuple[str, list[Line]]:
        """``(store, lines)``: the thread's newest ``limit`` lines, oldest
        first, and which store held them — ``store`` itself when the event
        named one. ``[]`` lines when no store has the thread. Synchronous:
        callers thread these off the loop."""
        ...

    def photos(self, store: str, line: Line) -> tuple[str, ...]:
        """Stash ``line``'s owner photos; their paths."""
        ...


class SeenCursors:
    """Per monitor and watched thread (``key``), the newest line id its last
    wake there showed, and the session it woke — so wiping that session can
    forget the cursor and the next wake starts with context again. Persisted
    as JSON; ``path=None`` keeps it in memory (tests)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._seen: dict[str, dict[str, object]] = self._load()

    def get(self, monitor_id: int, key: str) -> int | None:
        entry = self._seen.get(f"{monitor_id}\x00{key}")
        line = entry.get("line") if entry else None
        return line if isinstance(line, int) else None

    def set(self, monitor_id: int, key: str, line_id: int, wake_thread: str) -> None:
        self._seen[f"{monitor_id}\x00{key}"] = {
            "monitor": monitor_id, "line": line_id, "wake": wake_thread}
        self._save()

    def forget_wake(self, wake_thread: str) -> None:
        """Drop every cursor whose wake went to ``wake_thread`` (it was wiped)."""
        self._drop(lambda v: v.get("wake") == wake_thread)

    def forget_monitor(self, monitor_id: int) -> None:
        """Drop a monitor's cursors (deleted, or re-pointed at a new session)."""
        self._drop(lambda v: v.get("monitor") == monitor_id)

    def _drop(self, gone: Callable[[dict[str, object]], bool]) -> None:
        kept = {k: v for k, v in self._seen.items() if not gone(v)}
        if len(kept) != len(self._seen):
            self._seen = kept
            self._save()

    def _load(self) -> dict[str, dict[str, object]]:
        if self._path is None:
            return {}
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            # Losing cursors costs only a re-shown overlap, never a message.
            logger.exception("monitor cursors %s unreadable; fresh", self._path)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _save(self) -> None:
        if self._path is None:
            return
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._seen))
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("could not save monitor cursors to %s", self._path)


def payload_line(payload: dict[str, object], clock: Callable[[], float]) -> Line:
    """The event itself as a line, for a channel with no record of its own."""
    return Line(
        id=0, at=clock(), sender=str(payload.get("sender", "")),
        text=str(payload.get("text", "")),
    )
