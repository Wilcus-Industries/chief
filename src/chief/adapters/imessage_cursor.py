"""Where chief is in each Messages store it polls.

The persisted rowid cursor the at-most-once contract hangs on, and the
per-store pairing of a ``chat.db`` with its own cursor. Split from
:mod:`.imessage_store` — that module is the read layer (SQL and decoding),
this one is position.
"""

from dataclasses import dataclass
from pathlib import Path

from chief.adapters.imessage_store import PolledRow, fetch_rows, head_rowid

__all__ = ["RowCursor", "Store"]


class RowCursor:
    """The persisted rowid cursor; saving at read is the at-most-once contract."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> int:
        if self._path.exists():
            return int(self._path.read_text().strip() or 0)
        return 0

    def save(self, value: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(str(value))


@dataclass
class Store:
    """One ``chat.db`` chief polls, with its own cursor.

    Rowids are per-store, so two stores need two cursors — one shared cursor
    would skip rows in whichever store is behind. ``mine`` is False for the
    owner's store, which chief reads so the owner's existing monitors keep
    working; see the adapter for what it drops from there.
    """

    db_path: Path
    cursor: RowCursor
    position: int = 0
    mine: bool = True

    def prime(self) -> None:
        """Load the cursor, or start at the store's head — no history replay."""
        self.position = self.cursor.load() or head_rowid(self.db_path)
        self.cursor.save(self.position)

    def fetch(self, scope: frozenset[str]) -> list[PolledRow]:
        return fetch_rows(self.db_path, scope, self.position)

    def advance(self, rowid: int) -> None:
        self.position = rowid
        self.cursor.save(rowid)
