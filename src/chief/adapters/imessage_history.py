"""A thread's recent messages straight from ``chat.db``, for monitor wakes.

The poller reads only what chief should act on, so it never sees chief's own
sends; the history a wake shows must include them, or chief cannot tell what
it already said. This reads a whole chat — inbound and ``is_from_me`` rows —
by its ``chat_identifier``, which is what an iMessage ``thread_key`` is (a
group's chat id, or a one-to-one correspondent's handle).

Whose an ``is_from_me`` row is depends on the store: chief's own sends in a
dedicated store; the owner's in the owner's store, which dedicated mode also
reads; in a shared (``self`` mode) store it could be either — chief's only
when it carries the reply prefix, which most sends do not — so such a line is
marked ``shared`` and shown as "owner or you". An event names the store its
row came from, and only that store is read: a 1:1's identifier is the same
handle in both stores, so guessing would read the wrong conversation.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from chief.adapters.imessage_attachments import ImageStash
from chief.adapters.imessage_store import sent_at, text_of
from chief.monitors.history import Line
from chief.policy import normalize_handle

logger = logging.getLogger(__name__)

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
        prefix: str = "", stash: ImageStash | None = None,
    ) -> None:
        self._stores = tuple(stores)
        self._owner = owner_handles[0] if owner_handles else ""
        self._owners = frozenset(normalize_handle(h) for h in owner_handles)
        self._prefix = prefix.strip()
        self._stash = stash

    def recent(
        self, thread_key: str, limit: int, store: str = ""
    ) -> tuple[str, list[Line]]:
        named = [s for s in self._stores if str(s.db_path) == store]
        for candidate in named or ([] if store else self._stores):
            if lines := self._read(candidate, thread_key, limit):
                return str(candidate.db_path), lines
        return "", []

    def photos(self, store: str, line: Line) -> tuple[str, ...]:
        if self._stash is None or not line.owner_photos:
            return ()
        return self._stash.stash(Path(store), line.id)

    def _read(self, store: HistoryStore, thread_key: str, limit: int) -> list[Line]:
        try:
            conn = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(HISTORY_QUERY, (thread_key, limit)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            # Unreadable now (locked, Full Disk Access lost): the wake falls
            # back to the event alone, so say why.
            logger.warning("thread history unreadable in %s: %s", store.db_path, exc)
            return []
        lines = [self._line(store, *row) for row in reversed(rows)]
        return [line for line in lines if line.text]

    def _line(
        self, store: HistoryStore, rowid: int, sender: str, text: object,
        body: object, from_me: int, date: int | None,
    ) -> Line:
        said = text_of(text, body).strip()
        mine = shared = False
        if from_me:
            if store.from_me == "chief":
                mine = True
            elif store.from_me == "prefixed":
                if self._prefix and said.startswith(self._prefix):
                    mine, said = True, said[len(self._prefix):].strip()
                else:
                    shared = True
            sender = "" if mine else self._owner
        owner_photos = False
        if self._stash is not None and not mine:
            owner = normalize_handle(str(sender)) in self._owners
            labelled = self._stash.label(store.db_path, rowid, said, owner)
            said, owner_photos = labelled.text, labelled.owner_photos
        return Line(id=int(rowid), at=sent_at(int(date or 0)), sender=str(sender),
                    text=said.replace("￼", "").strip(), mine=mine,
                    owner_photos=owner_photos, shared=shared)
