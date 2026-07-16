"""Append-only audit log: every tool call and gate decision, as JSONL.

Non-negotiable for debugging a self-editing agent — if it did something,
the line is here.
"""

import json
from datetime import UTC, datetime
from pathlib import Path


class AuditLog:
    """Appends one JSON line per recorded action."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind: str, **data: object) -> None:
        """Append one entry. ``kind`` names the action, data is free-form."""
        entry = {"ts": datetime.now(UTC).isoformat(), "kind": kind, **data}
        with self._path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
