"""Turns in flight, on disk, so a restart that kills one doesn't lose it.

Every owner/system turn is recorded the moment it is queued, before it waits
on the thread lock, the concurrency cap or a pending restart. It is flagged
``started`` once its user message is in the transcript, and dropped when the
turn finishes (or fails: the dispatcher already told the owner). Whatever is
still listed when a process dies was killed by it:

- **not started**: held behind a restart drain or queued behind another
  turn. The message reached chief (the iMessage cursor is saved at read) but
  never reached history. The next boot replays it.
- **started**: cut off mid-turn (a drain timeout, ``chief stop``, a crash).
  The next boot wakes the thread to pick up or report where it stopped.

A cancelled turn keeps its record on purpose: in a turn, cancellation means
the daemon is going down. The fresh boot claims the leftovers at construction,
so its own new turns can't overwrite them before ``take_interrupted`` runs.
Records past :data:`STALE_SECONDS` are dropped instead of answered hours late.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

INFLIGHT_FILE = "inflight_turns.json"
STALE_SECONDS = 3600.0


@dataclass(frozen=True)
class Interrupted:
    """One turn the previous process died holding."""

    thread_key: str
    channel: str
    sender: str
    text: str
    started: bool


class _Ticket:
    def __init__(self, log: "InflightLog", turn_id: str) -> None:
        self._log = log
        self._id = turn_id

    def started(self) -> None:
        self._log._update(self._id, started=True)

    def done(self) -> None:
        self._log._remove(self._id)


class InflightLog:
    """The durable list of turns queued or running in this process."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / INFLIGHT_FILE
        self._entries = self._load()
        self._leftovers = list(self._entries)

    def track(self, thread_key: str, channel: str, sender: str, text: str) -> _Ticket:
        turn_id = uuid.uuid4().hex
        self._entries[turn_id] = {
            "thread_key": thread_key, "channel": channel, "sender": sender,
            "text": text, "started": False, "at": time.time(),
        }
        self._save()
        return _Ticket(self, turn_id)

    def take_interrupted(self) -> list[Interrupted]:
        """The previous process's unfinished turns, oldest first; once."""
        taken = [self._entries.pop(i) for i in self._leftovers if i in self._entries]
        self._leftovers = []
        if taken:
            self._save()
        return [
            Interrupted(
                str(e["thread_key"]), str(e["channel"]), str(e["sender"]),
                str(e["text"]), bool(e["started"]),
            )
            for e in taken
        ]

    def _update(self, turn_id: str, **fields: Any) -> None:
        if turn_id in self._entries:
            self._entries[turn_id].update(fields)
            self._save()

    def _remove(self, turn_id: str) -> None:
        if self._entries.pop(turn_id, None) is not None:
            self._save()

    def _load(self) -> dict[str, dict[str, Any]]:
        """Never raises: a corrupt record must not block a boot."""
        try:
            data = json.loads(self._path.read_text())
            now = time.time()
            fresh = {
                k: v for k, v in data.items()
                if now - float(v["at"]) <= STALE_SECONDS
            }
        except FileNotFoundError:
            return {}
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.exception("unreadable in-flight turn record at %s", self._path)
            return {}
        if len(fresh) < len(data):
            dropped = len(data) - len(fresh)
            logger.warning("dropping %d stale in-flight turn(s)", dropped)
        return fresh

    def _save(self) -> None:
        """Atomic replace; a full disk is logged, never raised into a turn."""
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._entries))
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("could not record in-flight turns at %s", self._path)
