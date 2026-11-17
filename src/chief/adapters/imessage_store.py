"""Messages ``chat.db`` read layer: poll query + executor, cursor, decoding.

Split out of ``imessage.py`` to keep that file under the length cap. The
concerns living here:

* the SQL that selects candidate rows past the cursor, and the read-only
  sqlite executors that run it (:func:`fetch_rows`, :func:`head_rowid`);
* where chief is in each store — :mod:`.imessage_cursor`;
* reading a row's ``date`` as when it was sent (:func:`sent_at`);
* extracting a row's text — which on modern macOS is NOT always in
  ``message.text``. The owner's own sends (``is_from_me = 1``, i.e. every
  self-DM to chief) store their body only in ``message.attributedBody``, an
  Apple ``streamtyped`` (NSAttributedString) blob with ``text`` left NULL. A
  poller that read ``text`` alone silently dropped every self-DM.
"""

import sqlite3
import time
from datetime import UTC, datetime
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
    "message.attributedBody AS body, message.date AS date "
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
DEDUP_WINDOW_SECONDS = 5.0

# ``message.date`` counts from 2001-01-01 UTC, not 1970. Nothing chief polls
# predates iMessage, so an earlier date is unset or corrupt.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC).timestamp()
IMESSAGE_LAUNCH = datetime(2011, 10, 12, tzinfo=UTC).timestamp()

_NSSTRING = b"NSString"


class RecentDedup:
    """Collapses self-DM twin rows to one delivery.

    A ``(thread_key, sender, text)`` key seen again within ``window`` seconds of
    its last sighting is a redelivery of the same logical message — skip it. The
    thread is part of the key so two people saying "ok" in different group
    chats at once stay two messages. The window keeps a genuine later repeat of
    the same text (owner types it again minutes on) from being swallowed. State
    is in-memory: a fresh boot re-primes from the store cursor, never replaying
    an already-delivered twin.

    Callers pass when the row was sent *after* :func:`sent_at` has made it a
    usable time. A row with no usable date (NULL, 0) is placed at the poll time,
    never at its raw date: that is 2001, before every real row, and a negative
    gap counts as inside the window, so a genuine repeat hours later was dropped.
    """

    def __init__(self, window: float = DEDUP_WINDOW_SECONDS) -> None:
        self._window = window
        self._seen: dict[tuple[str, ...], float] = {}

    def is_duplicate(self, key: tuple[str, ...], at: float) -> bool:
        """True when ``key`` was last seen within the window of ``at`` (epoch
        seconds); either way ``at`` becomes its last sighting."""
        self._seen = {k: t for k, t in self._seen.items() if at - t <= self._window}
        prev = self._seen.get(key)
        self._seen[key] = at
        return prev is not None and at - prev <= self._window


def sent_at(date: int) -> float:
    """When a polled row was sent, as epoch seconds, from its ``message.date``.

    The unit is whichever makes the date a real moment: nanoseconds on a store
    written since macOS 10.13, seconds in the older rows an upgraded store kept
    (the two ranges never overlap). A date that is neither — unset (0),
    negative, from before iMessage, or in the future — gives the time now:
    chief cannot say when it was sent, only that it has it."""
    now = time.time()
    for per_second in (1e9, 1.0):
        sent = APPLE_EPOCH + date / per_second
        if IMESSAGE_LAUNCH <= sent <= now:
            return sent
    return now


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


#: One polled row: (rowid, sender, text, from_me, group_chat, in_self, date);
#: ``date`` is the raw ``message.date``, 0 when the store has none.
PolledRow = tuple[int, str, str, int, str | None, int, int]


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
                None if r[4] is None else str(r[4]), int(r[5]), int(r[7] or 0),
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
