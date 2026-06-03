"""Append-only JSONL audit log (DESIGN: Ops & observability).

Every tool call, approval decision, and (from M4) memory write appends one JSON object
per line to a single file. JSONL keeps the trail greppable and append-only — no row is
ever rewritten — so it doubles as a tamper-evident record of what the agent did and what
the owner approved. M3 is the first milestone that produces auditable events, so the
sink lands here; later milestones call :meth:`AuditLog.log` with their own event dicts.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    """Append events as JSON lines to a file, creating parent dirs on first write."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def log(self, event: dict[str, Any]) -> None:
        """Append one ``{ts, **event}`` line. ``ts`` is server time (UTC, ISO-8601)."""
        record = {"ts": datetime.now(UTC).isoformat(), **event}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
