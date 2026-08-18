"""Messages ``chat.db`` read layer: poll query + executor, cursor, decoding.

Split out of ``imessage.py`` to keep that file under the length cap. The
concerns living here:

* the SQL that selects candidate rows past the cursor, and the read-only
  sqlite executors that run it (:func:`fetch_rows`, :func:`head_rowid`);
* where chief is in each store — :mod:`.imessage_cursor`;
* extracting a row's text — which on modern macOS is NOT always in
  ``message.text``. The owner's own sends (``is_from_me = 1``, i.e. every
  self-DM to chief) store their body only in ``message.attributedBody``, an
  Apple ``streamtyped`` (NSAttributedString) blob with ``text`` left NULL. A
  poller that read ``text`` alone silently dropped every self-DM.
"""

import sqlite3
from pathlib import Path

# Candidate rows past the cursor. A row qualifies when it is a real inbound
# text (is_from_me = 0) OR it lives in the owner's self-chat
# (chat.chat_identifier IN owner_handles) — that scope is what lets the owner
# DM their own assistant from their own Apple ID without leaking their other
# conversations, since owner->friend sends carry the friend's chat, not the
# owner's. It also means chief's own group sends (is_from_me = 1, outside the
# self-chat) never poll back, so a group needs no BOT_PREFIX echo guard.
# associated_message_type = 0 drops tapbacks/edits. ``group_chat`` carries a
# group's chat_identifier and is NULL for one-to-one chats — one column for
# both "is this a group" and "which group", so a group message can thread on
# the conversation instead of on whoever spoke. A body counts when text is
# present OR an attributedBody blob is (self-DMs carry only the latter).
# Handles bind as parameters ({scope} is only placeholder count).
POLL_QUERY = (
    "SELECT message.ROWID AS rowid, handle.id AS sender, message.text AS text, "
    "message.is_from_me AS from_me, "
    "MAX(CASE WHEN (chat.style IS NOT NULL AND chat.style != 45) "
    "OR chat.room_name IS NOT NULL THEN chat.chat_identifier END) AS group_chat, "
    "MAX(CASE WHEN self_chat.mid IS NOT NULL THEN 1 ELSE 0 END) AS in_self, "
    "message.attributedBody AS body, message.date AS date, "
    "message.guid AS guid "
    "FROM message JOIN handle ON message.handle_id = handle.ROWID "
    "LEFT JOIN chat_message_join ON chat_message_join.message_id = message.ROWID "
    "LEFT JOIN chat ON chat.ROWID = chat_message_join.chat_id "
    "LEFT JOIN (SELECT j.message_id AS mid FROM chat_message_join j "
    "JOIN chat c ON c.ROWID = j.chat_id WHERE c.chat_identifier IN ({scope})) "
    "self_chat ON self_chat.mid = message.ROWID "
    "WHERE message.ROWID > ? AND message.associated_message_type = 0 "
    "AND ((message.text IS NOT NULL AND message.text != '') "
    "OR message.attributedBody IS NOT NULL) "
    "AND (message.is_from_me = 0 OR self_chat.mid IS NOT NULL) "
    "GROUP BY message.ROWID ORDER BY message.ROWID ASC LIMIT ?"
)
HEAD_QUERY = "SELECT COALESCE(MAX(ROWID), 0) FROM message"
POLL_BATCH_LIMIT = 200

# macOS records one owner self-DM as several rows — an is_from_me=1 send plus
# one (sometimes more) is_from_me=0 receive twins, same text, dates within tens
# of ms, different guids. Delivering each would run a turn per copy, so chief
# answers the one message twice. Dedup by (sender, text) over this window.
DEDUP_WINDOW_NS = 5_000_000_000  # 5s

_NSSTRING = b"NSString"


class RecentDedup:
    """Collapses self-DM twin rows to one delivery.

    A ``(thread_key, sender, text)`` key seen again within ``window_ns`` of its
    last sighting is a redelivery of the same logical message — skip it. The
    thread is part of the key so two people saying "ok" in different group
    chats at once stay two messages. The window keeps a genuine later repeat of
    the same text (owner types it again minutes on) from being swallowed. State
    is in-memory: a fresh boot re-primes from the store cursor, never replaying
    an already-delivered twin.

    Also used, keyed on ``guid`` alone, for the other duplicate: one message
    present in both stores. That one is exact rather than heuristic — the two
    copies *are* the same message, so they carry the same guid, where self-DM
    twins carry different ones. Read skew between the stores cannot defeat it
    either: the window is measured on the row's own date, which is identical
    in both.
    """

    def __init__(self, window_ns: int = DEDUP_WINDOW_NS) -> None:
        self._window = window_ns
        self._seen: dict[tuple[str, ...], int] = {}

    def is_duplicate(self, key: tuple[str, ...], date_ns: int) -> bool:
        self._seen = {
            k: d for k, d in self._seen.items() if date_ns - d <= self._window
        }
        prev = self._seen.get(key)
        self._seen[key] = date_ns
        return prev is not None and date_ns - prev <= self._window


def decode_attributed_body(data: bytes) -> str:
    """Pull the plain text out of a ``streamtyped`` attributedBody blob.

    The text is a length-prefixed UTF-8 run right after the ``NSString`` class
    marker: a ``+`` (0x2b) token, then the length as a single byte, or 0x81
    followed by a little-endian uint16, or 0x82 followed by a uint32. Anything
    unrecognised decodes to "" — callers treat an empty body as no message.
    """
    marker = data.find(_NSSTRING)
    if marker < 0:
        return ""
    plus = data.find(b"+", marker + len(_NSSTRING))
    if plus < 0 or plus + 1 >= len(data):
        return ""
    i = plus + 1
    length = data[i]
    i += 1
    if length == 0x81:
        length = int.from_bytes(data[i : i + 2], "little")
        i += 2
    elif length == 0x82:
        length = int.from_bytes(data[i : i + 4], "little")
        i += 4
    return data[i : i + length].decode("utf-8", "replace")


def text_of(text: object, body: object) -> str:
    """Row text: ``message.text`` when set, else the decoded attributedBody."""
    if text:
        return str(text)
    if isinstance(body, (bytes, bytearray)):
        return decode_attributed_body(bytes(body))
    return ""


#: One polled row: (rowid, sender, text, from_me, group_chat, in_self, date).
PolledRow = tuple[int, str, str, int, str | None, int, int, str]


def fetch_rows(
    db_path: Path, scope_handles: frozenset[str], after: int
) -> list[PolledRow]:
    """Run :data:`POLL_QUERY` read-only and coerce the rows (sync; callers
    thread it off the loop).

    ``scope_handles`` are the self-chat identifiers; empty (dedicated mode)
    turns that scope off, leaving only real inbound rows."""
    handles = tuple(scope_handles)
    scope = ",".join("?" for _ in handles) if handles else "NULL"
    query = POLL_QUERY.format(scope=scope)
    params: tuple[object, ...] = (*handles, after, POLL_BATCH_LIMIT)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(query, params)
        return [
            (
                int(r[0]), str(r[1]), text_of(r[2], r[6]), int(r[3]),
                None if r[4] is None else str(r[4]), int(r[5]), int(r[7]),
                str(r[8] or ""),
            )
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def head_rowid(db_path: Path) -> int:
    """The store's current max rowid — where a first boot starts (no replay)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return int(conn.execute(HEAD_QUERY).fetchone()[0])
    finally:
        conn.close()
