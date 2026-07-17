"""iMessage adapter over a fake chat.db: cursor, mapping, echo, send."""

import sqlite3
from pathlib import Path

from chief.adapters.base import Message
from chief.adapters.imessage import BOT_PREFIX, IMessageAdapter

OWNER = "+15550001111"

# A real streamtyped attributedBody blob from a macOS chat.db (is_from_me=1,
# text column NULL). Decodes to "I’ll take the edi too" (curly apostrophe).
SELF_DM_BODY = bytes.fromhex(
    "040b73747265616d747970656481e803840140848484124e534174747269627574"
    "6564537472696e67008484084e534f626a656374008592848484084e5353747269"
    "6e67019484012b1749e280996c6c2074616b65207468652065646920746f6f8684"
    "0269490115928484840c4e5344696374696f6e617279009484016901928496961d"
    "5f5f6b494d4d657373616765506172744174747269627574654e616d6586928484"
    "84084e534e756d626572008484074e5356616c7565009484012a84999900868686"
)

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, handle_id INTEGER, text TEXT,
    is_from_me INTEGER DEFAULT 0, associated_message_type INTEGER DEFAULT 0,
    attributedBody BLOB
);
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY, style INTEGER, room_name TEXT,
    chat_identifier TEXT
);
CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
"""


class FakeStore:
    """A chat.db lookalike the adapter polls."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)

    def add_message(
        self,
        sender: str,
        text: str,
        *,
        from_me: int = 0,
        tapback: int = 0,
        group: bool = False,
        chat: str | None = None,
        body: bytes | None = None,
    ) -> None:
        """Insert one message, optionally in a direct chat (``chat`` =
        the chat_identifier) or a group. ``chat`` models the self-chat
        when it equals an owner handle. ``body`` sets attributedBody — how
        modern macOS stores the owner's own sends, with ``text`` left empty."""
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT ROWID FROM handle WHERE id = ?", (sender,)
            ).fetchone()
            handle_id = (
                row[0]
                if row
                else conn.execute(
                    "INSERT INTO handle (id) VALUES (?)", (sender,)
                ).lastrowid
            )
            msg_id = conn.execute(
                "INSERT INTO message (handle_id, text, is_from_me, "
                "associated_message_type, attributedBody) VALUES (?, ?, ?, ?, ?)",
                (handle_id, text or None, from_me, tapback, body),
            ).lastrowid
            chat_id: int | None = None
            if group:
                chat_id = conn.execute(
                    "INSERT INTO chat (style, room_name, chat_identifier) "
                    "VALUES (43, 'room', 'group;+;room')"
                ).lastrowid
            elif chat is not None:
                found = conn.execute(
                    "SELECT ROWID FROM chat WHERE chat_identifier = ?", (chat,)
                ).fetchone()
                chat_id = (
                    found[0]
                    if found
                    else conn.execute(
                        "INSERT INTO chat (style, room_name, chat_identifier) "
                        "VALUES (45, NULL, ?)",
                        (chat,),
                    ).lastrowid
                )
            if chat_id is not None:
                conn.execute(
                    "INSERT INTO chat_message_join (message_id, chat_id) "
                    "VALUES (?, ?)",
                    (msg_id, chat_id),
                )


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.store = FakeStore(tmp_path / "chat.db")
        self.delivered: list[Message] = []
        self.jxa_calls: list[tuple[str, tuple[str, ...]]] = []
        self.cursor_path = tmp_path / "cursor"

    def adapter(self) -> IMessageAdapter:
        async def on_message(message: Message) -> None:
            self.delivered.append(message)

        async def run_jxa(script: str, argv: tuple[str, ...]) -> str:
            self.jxa_calls.append((script, argv))
            return "sent"

        return IMessageAdapter(
            on_message,
            db_path=self.store.path,
            cursor_path=self.cursor_path,
            owner_handles=(OWNER,),
            run_jxa=run_jxa,
        )


async def test_first_boot_starts_at_head_no_replay(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "ancient history")
    adapter = harness.adapter()
    await adapter.start()
    await adapter.stop()
    harness.store.add_message(OWNER, "fresh text")
    await adapter.poll_once()
    assert [m.text for m in harness.delivered] == ["fresh text"]


async def test_owner_maps_stranger_passes_echo_and_noise_skip(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "hi chief")  # dedicated-mode inbound
    harness.store.add_message("+15559998888", "yo from a stranger")
    # chief's real echo: from_me=1, in the self-chat, 🤖-prefixed.
    harness.store.add_message(
        OWNER, BOT_PREFIX + "hi yourself", from_me=1, chat=OWNER
    )
    harness.store.add_message(OWNER, "loved a message", tapback=2000)
    # owner->friend sent copy: from_me=1, NOT the self-chat -> never dispatch.
    harness.store.add_message("+15551112222", "sent copy", from_me=1)
    harness.store.add_message("+15557776666", "group chatter", group=True)
    adapter = harness.adapter()
    await adapter.poll_once()
    assert [(m.sender, m.thread_key, m.text) for m in harness.delivered] == [
        ("owner", OWNER, "hi chief"),
        ("+15559998888", "+15559998888", "yo from a stranger"),
    ]
    assert all(m.channel == "imessage" for m in harness.delivered)


async def test_owner_self_dm_from_me_delivered(tmp_path: Path) -> None:
    """Same-account self-DM: the owner texts their own self-chat, so the
    row is from_me=1 yet must run a turn as sender 'owner'."""
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "note to self", from_me=1, chat=OWNER)
    adapter = harness.adapter()
    await adapter.poll_once()
    assert [(m.sender, m.thread_key, m.text) for m in harness.delivered] == [
        ("owner", OWNER, "note to self"),
    ]


async def test_self_dm_body_in_attributedbody_is_decoded(tmp_path: Path) -> None:
    """The owner's own sends store their text only in attributedBody (text
    NULL). A real self-DM blob must decode and dispatch, not get dropped."""
    harness = Harness(tmp_path)
    harness.store.add_message(
        OWNER, "", from_me=1, chat=OWNER, body=SELF_DM_BODY
    )
    adapter = harness.adapter()
    await adapter.poll_once()
    assert [(m.sender, m.text) for m in harness.delivered] == [
        ("owner", "I’ll take the edi too"),
    ]


async def test_self_chat_scope_does_not_leak_other_conversations(
    tmp_path: Path,
) -> None:
    """A from_me=1 message in a NON-self chat (owner texting a friend) must
    never dispatch, even though a self-chat from_me=1 in the same poll does."""
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "self note", from_me=1, chat=OWNER)
    harness.store.add_message(
        "+15554443333", "hey friend", from_me=1, chat="+15554443333"
    )
    adapter = harness.adapter()
    await adapter.poll_once()
    assert [(m.sender, m.text) for m in harness.delivered] == [
        ("owner", "self note"),
    ]


async def test_cursor_persists_across_restarts(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "first")
    adapter = harness.adapter()
    await adapter.poll_once()
    assert len(harness.delivered) == 1

    reborn = harness.adapter()
    await reborn.start()
    await reborn.stop()
    harness.store.add_message(OWNER, "second")
    await reborn.poll_once()
    assert [m.text for m in harness.delivered] == ["first", "second"]


async def test_send_prefixes_owner_threads_only(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    adapter = harness.adapter()
    await adapter.send(OWNER, "reply to self-chat")
    await adapter.send("+15559998888", "owner-requested text")
    assert harness.jxa_calls[0][1] == (OWNER, BOT_PREFIX + "reply to self-chat")
    assert harness.jxa_calls[1][1] == ("+15559998888", "owner-requested text")
