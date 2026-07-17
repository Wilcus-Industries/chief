"""Messages ``chat.db`` read layer: the poll query and body decoding.

Split out of ``imessage.py`` to keep that file under the length cap. Two
concerns live here:

* the SQL that selects candidate rows past the cursor, and
* extracting a row's text — which on modern macOS is NOT always in
  ``message.text``. The owner's own sends (``is_from_me = 1``, i.e. every
  self-DM to chief) store their body only in ``message.attributedBody``, an
  Apple ``streamtyped`` (NSAttributedString) blob with ``text`` left NULL. A
  poller that read ``text`` alone silently dropped every self-DM.
"""

# Candidate rows past the cursor. A row qualifies when it is a real inbound
# text (is_from_me = 0) OR it lives in the owner's self-chat
# (chat.chat_identifier IN owner_handles) — that scope is what lets the owner
# DM their own assistant from their own Apple ID without leaking their other
# conversations, since owner->friend sends carry the friend's chat, not the
# owner's. associated_message_type = 0 drops tapbacks/edits; the chat joins
# expose group/room flags plus self-chat membership. A body counts when text
# is present OR an attributedBody blob is (self-DMs carry only the latter).
# Handles bind as parameters ({scope} is only placeholder count).
POLL_QUERY = (
    "SELECT message.ROWID AS rowid, handle.id AS sender, message.text AS text, "
    "message.is_from_me AS from_me, "
    "MAX(CASE WHEN chat.style IS NOT NULL AND chat.style != 45 "
    "THEN 1 ELSE 0 END) AS in_group, "
    "MAX(CASE WHEN chat.room_name IS NOT NULL THEN 1 ELSE 0 END) AS has_room, "
    "MAX(CASE WHEN self_chat.mid IS NOT NULL THEN 1 ELSE 0 END) AS in_self, "
    "message.attributedBody AS body "
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

_NSSTRING = b"NSString"


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
