"""iMessage adapter over a fake chat.db: cursor, mapping, echo, send."""

import sqlite3
from pathlib import Path

from chief.adapters.base import Message
from chief.adapters.imessage import BOT_PREFIX, IMessageAdapter

OWNER = "+15550001111"

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, handle_id INTEGER, text TEXT,
    is_from_me INTEGER DEFAULT 0, associated_message_type INTEGER DEFAULT 0
);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, style INTEGER, room_name TEXT);
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
    ) -> None:
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
                "associated_message_type) VALUES (?, ?, ?, ?)",
                (handle_id, text, from_me, tapback),
            ).lastrowid
            if group:
                chat_id = conn.execute(
                    "INSERT INTO chat (style, room_name) VALUES (43, 'room')"
                ).lastrowid
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
    harness.store.add_message(OWNER, "hi chief")
    harness.store.add_message("+15559998888", "yo from a stranger")
    harness.store.add_message(OWNER, BOT_PREFIX + "hi yourself")  # own echo
    harness.store.add_message(OWNER, "loved a message", tapback=2000)
    harness.store.add_message(OWNER, "sent copy", from_me=1)
    harness.store.add_message("+15557776666", "group chatter", group=True)
    adapter = harness.adapter()
    await adapter.poll_once()
    assert [(m.sender, m.thread_key, m.text) for m in harness.delivered] == [
        ("owner", OWNER, "hi chief"),
        ("+15559998888", "+15559998888", "yo from a stranger"),
    ]
    assert all(m.channel == "imessage" for m in harness.delivered)


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
