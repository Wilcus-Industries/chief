"""Messages in flight, on disk, so a process death that kills one doesn't lose it.

Every owner/system message is recorded the moment it reaches chief, by whoever
first holds it: the iMessage FIFO at enqueue (the cursor is already saved),
the dispatcher at ``handle`` entry, an inject at the running turn's inbox. The
record is flagged ``started`` once its user message is in the transcript, and
dropped once it is answered (or failed: the dispatcher already told the owner).
Whatever is still listed when a process dies was killed by it:

- **not started**: queued, injected or held behind a restart drain. The
  message reached chief but never reached history. The next boot replays it.
- **started**: cut off mid-turn (a drain timeout, ``chief stop``, a crash).
  The next boot wakes the thread to pick up or report where it stopped.

A cancelled turn keeps its record on purpose: in a turn, cancellation means
the daemon is going down. The fresh boot notes the leftovers at construction;
they stay on disk, and so survive a death mid-resume, until the resume
``adopt``s each one and its replay ends it. An adopted record that dies again
is given up on at the next boot (logged), so a turn that kills the process
can't crash-loop it (one that never got to start, held behind another
restart, is not counted). Records past :data:`STALE_SECONDS` since the
message first arrived are dropped instead of answered hours late.
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
MAX_ATTEMPTS = 1  # resumes per record; one more death and it is dropped
_FIELDS = {"thread_key": str, "channel": str, "sender": str, "text": str,
           "started": bool, "at": float, "attempts": int}


@dataclass(frozen=True)
class Interrupted:
    """One message the previous process died holding."""

    turn_id: str
    thread_key: str
    channel: str
    sender: str
    text: str
    started: bool


class _Ticket:
    def __init__(self, log: "InflightLog", turn_id: str, *, resumed: bool = False):
        self._log = log
        self._id = turn_id
        self._resumed = resumed

    def started(self) -> None:
        self._log._started(self._id, self._resumed)

    def done(self) -> None:
        self._log.forget(self._id)


class InflightLog:
    """The durable list of messages chief holds unanswered."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / INFLIGHT_FILE
        self._entries = self._load()
        self._leftovers = list(self._entries)

    def track(self, thread_key: str, channel: str, sender: str, text: str) -> _Ticket:
        turn_id = uuid.uuid4().hex
        self._entries[turn_id] = {
            "thread_key": thread_key, "channel": channel, "sender": sender,
            "text": text, "started": False, "at": time.time(), "attempts": 0,
        }
        self._save()
        return _Ticket(self, turn_id)

    def take_interrupted(self) -> list[Interrupted]:
        """The previous process's unfinished messages, oldest first; once.
        They stay recorded until ``adopt``ed and ended, or ``forget``-ten."""
        taken = [(i, self._entries[i]) for i in self._leftovers if i in self._entries]
        self._leftovers = []
        return [
            Interrupted(i, e["thread_key"], e["channel"], e["sender"], e["text"],
                        e["started"])
            for i, e in taken
        ]

    def adopt(self, turn_id: str) -> _Ticket:
        """Take over a leftover for its resume, keeping its arrival time. The
        attempt counts only once the resumed turn starts: one held at the gate
        by a restart never ran, so dying there is no strike against it."""
        return _Ticket(self, turn_id, resumed=True)

    def forget(self, turn_id: str) -> None:
        if self._entries.pop(turn_id, None) is not None:
            self._save()

    def _started(self, turn_id: str, resumed: bool) -> None:
        if (entry := self._entries.get(turn_id)) is not None:
            entry["started"] = True
            entry["attempts"] += resumed
            self._save()

    def _load(self) -> dict[str, dict[str, Any]]:
        """Never raises: a bad record is dropped, never allowed to block a boot."""
        try:
            data = json.loads(self._path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.exception("unreadable in-flight record at %s", self._path)
            return {}
        now = time.time()
        kept: dict[str, dict[str, Any]] = {}
        for turn_id, entry in data.items():
            if not _well_formed(entry):
                logger.warning("dropping malformed in-flight record %s", turn_id)
            elif now - entry["at"] > STALE_SECONDS:
                logger.warning("dropping stale in-flight message on %s",
                               entry["thread_key"])
            elif entry["attempts"] >= MAX_ATTEMPTS:
                logger.error("giving up on a message on %s: its resume died too",
                             entry["thread_key"])
            else:
                kept[turn_id] = entry
        return kept

    def _save(self) -> None:
        """Atomic replace; a full disk is logged, never raised into a turn."""
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._entries))
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("could not record in-flight messages at %s", self._path)


def _well_formed(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    for key, kind in _FIELDS.items():
        value = entry.get(key)
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            entry[key] = value = float(value)
        if kind is int and isinstance(value, bool):
            return False
        if not isinstance(value, kind):
            return False
    return True
