"""A thread's recent messages straight from ``chat.db``, for monitor wakes.

The poller reads only what chief should act on, so it never sees chief's own
sends; the history a wake shows must include them, or chief cannot tell what
it already said. This reads a whole chat — inbound and ``is_from_me`` rows —
by its ``chat_identifier``, which is what an iMessage ``thread_key`` is (a
group's chat id, or a one-to-one correspondent's handle).

Whose an ``is_from_me`` row is depends on the store: chief's own sends in a
dedicated store; in a shared (``self`` mode) store the owner's, unless it
carries the reply prefix; the owner's in the owner's store, which dedicated
mode also reads. The first store holding the chat wins.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from chief.adapters.imessage_store import sent_at, text_of
from chief.monitors.history import Line

# Newest first so LIMIT keeps the tail; reversed into oldest-first after.
# associated_message_type = 0 drops tapbacks and edits, as the poller does.
HISTORY_QUERY = (
    "SELECT message.ROWID, COALESCE(handle.id, ''), message.text, "
    "message.attributedBody, message.is_from_me, message.date "
    "FROM message "
    "JOIN chat_message_join ON chat_message_join.message_id = message.ROWID "
    "JOIN chat ON chat.ROWID = chat_message_join.chat_id "
    "LEFT JOIN handle ON handle.ROWID = message.handle_id "
    "WHERE chat.chat_identifier = ? AND message.associated_message_type = 0 "
    "GROUP BY message.ROWID ORDER BY message.ROWID DESC LIMIT ?"
)
OBJECT_MARK = "￼"  # where an attachment sits in a row's text


@dataclass(frozen=True)
class HistoryStore:
    """One ``chat.db`` and whose its ``is_from_me`` rows are: ``"chief"``,
    ``"owner"``, or ``"prefixed"`` (chief's when they carry ``prefix``)."""

    db_path: Path
    from_me: str


class IMessageHistory:
    """:class:`~chief.monitors.history.HistorySource` over Messages stores."""

    def __init__(
        self, stores: Sequence[HistoryStore], owner_handles: Sequence[str] = (),
        prefix: str = "",
    ) -> None:
        self._stores = tuple(stores)
        self._owner = owner_handles[0] if owner_handles else ""
        self._prefix = prefix

    def recent(self, thread_key: str, limit: int) -> list[Line]:
        for store in self._stores:
            if lines := self._read(store, thread_key, limit):
                return lines
        return []

    def _read(self, store: HistoryStore, thread_key: str, limit: int) -> list[Line]:
        try:
            conn = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute(HISTORY_QUERY, (thread_key, limit)).fetchall()
        except sqlite3.Error:
            return []  # unreadable now (locked, no access): the wake falls back
        finally:
            conn.close()
        lines = [self._line(store, *row) for row in reversed(rows)]
        return [line for line in lines if line.text]

    def _line(
        self, store: HistoryStore, rowid: int, sender: str, text: object,
        body: object, from_me: int, date: int | None,
    ) -> Line:
        said = text_of(text, body).replace(OBJECT_MARK, "").strip()
        mine = False
        if from_me:
            if store.from_me == "chief":
                mine = True
            elif store.from_me == "prefixed" and self._prefix and said.startswith(
                self._prefix.strip()
            ):
                mine, said = True, said[len(self._prefix.strip()):].strip()
            sender = "" if mine else self._owner
        return Line(id=int(rowid), at=sent_at(int(date or 0)), sender=str(sender),
                    text=said, mine=mine)
