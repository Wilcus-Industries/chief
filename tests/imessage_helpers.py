"""Shared iMessage test fixtures (#156): a schema-true chat.db + a fixture runner.

The PRD's central inbound mechanism is the real Messages store, so the CI suite
never mocks queries: :func:`make_chat_db` builds a real sqlite file with the
authentic ``chat.db`` schema (the tables/columns macOS writes — captured from the
real Mac; the env-gated live suite re-validates the shapes so they can't drift),
and :class:`FixtureRunner` sits at the exact :class:`ScriptRunner` subprocess seam:
a ``sqlite3`` invocation *executes the real query* against the fixture store
(stdlib sqlite3, read-only, ``-json``-shaped output), while ``osascript`` send
invocations are recorded and replayed — the outbound boundary the CI suite asserts
argv-for-argv.
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chief.adapters.base import Attachment, Surface
from chief.persistence.models import Task, Watch
from chief.tools.apple.runner import ScriptResult, ScriptRunner

#: Apple epoch (2001-01-01) offset from the unix epoch, in seconds.
APPLE_EPOCH_OFFSET = 978307200

#: The authentic ``chat.db`` schema subset: every table/column the adapter's SQL
#: touches, with the real names, types, and defaults macOS writes. The live suite
#: (tests/test_imessage_live.py) runs the same queries against the genuine store,
#: which is what keeps this fixture honest.
CHAT_DB_SCHEMA = """
CREATE TABLE handle (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE,
    id TEXT NOT NULL,
    country TEXT,
    service TEXT NOT NULL DEFAULT 'iMessage',
    uncanonicalized_id TEXT,
    person_centric_id TEXT,
    UNIQUE (id, service)
);
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE,
    guid TEXT UNIQUE NOT NULL,
    style INTEGER,
    state INTEGER,
    account_id TEXT,
    chat_identifier TEXT,
    service_name TEXT,
    room_name TEXT,
    display_name TEXT
);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE,
    guid TEXT UNIQUE NOT NULL,
    text TEXT,
    attributedBody BLOB,
    handle_id INTEGER DEFAULT 0,
    service TEXT,
    date INTEGER,
    date_read INTEGER DEFAULT 0,
    date_delivered INTEGER DEFAULT 0,
    is_from_me INTEGER DEFAULT 0,
    item_type INTEGER DEFAULT 0,
    associated_message_type INTEGER DEFAULT 0,
    cache_has_attachments INTEGER DEFAULT 0
);
CREATE TABLE chat_message_join (
    chat_id INTEGER REFERENCES chat (ROWID),
    message_id INTEGER REFERENCES message (ROWID),
    message_date INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE chat_handle_join (
    chat_id INTEGER REFERENCES chat (ROWID),
    handle_id INTEGER REFERENCES handle (ROWID),
    UNIQUE (chat_id, handle_id)
);
"""

#: ``chat.style`` values macOS writes: 45 = a 1:1 DM chat, 43 = a group chat.
STYLE_DM = 45
STYLE_GROUP = 43


def apple_ns(when: datetime) -> int:
    """A wall-clock instant as Apple-epoch nanoseconds (the ``message.date`` unit)."""
    return int((when.timestamp() - APPLE_EPOCH_OFFSET) * 1_000_000_000)


class ChatDb:
    """A writable schema-true fixture store, addressed like the real chat.db."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with sqlite3.connect(path) as conn:
            conn.executescript(CHAT_DB_SCHEMA)

    def add_handle(self, handle: str) -> int:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO handle (id, service) VALUES (?, 'iMessage')", (handle,)
            )
            return int(cur.lastrowid or 0)

    def add_chat(self, identifier: str, *, style: int = STYLE_DM,
                 room_name: str | None = None) -> int:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO chat (guid, style, chat_identifier, service_name, "
                "room_name) VALUES (?, ?, ?, 'iMessage', ?)",
                (f"iMessage;-;{identifier}", style, identifier, room_name),
            )
            return int(cur.lastrowid or 0)

    def add_message(
        self,
        *,
        handle_rowid: int,
        chat_rowid: int | None = None,
        text: str | None,
        is_from_me: bool = False,
        when: datetime | None = None,
        associated_message_type: int = 0,
    ) -> int:
        when = when or datetime.now(UTC)
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO message (guid, text, handle_id, service, date, "
                "is_from_me, associated_message_type) "
                "VALUES (?, ?, ?, 'iMessage', ?, ?, ?)",
                (
                    f"msg-{handle_rowid}-{apple_ns(when)}",
                    text,
                    handle_rowid,
                    apple_ns(when),
                    1 if is_from_me else 0,
                    associated_message_type,
                ),
            )
            message_rowid = int(cur.lastrowid or 0)
            # A chat_id-NULL row (no chat_message_join) is what the self-chat's
            # received copy looks like — the LEFT JOIN still classifies it a DM.
            if chat_rowid is not None:
                conn.execute(
                    "INSERT INTO chat_message_join (chat_id, message_id) "
                    "VALUES (?, ?)",
                    (chat_rowid, message_rowid),
                )
            return message_rowid

    def add_self_text(
        self,
        *,
        handle_rowid: int,
        chat_rowid: int,
        text: str,
        when: datetime | None = None,
    ) -> int:
        """Insert the self-chat message *pair* the real Mac writes (#161).

        A message the owner sends to their own number lands as two rows: a sent copy
        (``is_from_me=1``, ``text=None``, joined to the chat — rig rows 637/638) then
        a received copy (``is_from_me=0``, the text, *no* chat_message_join so
        ``chat_id`` is NULL — rig rows 615/616). Returns the received copy's ROWID.
        """
        self.add_message(
            handle_rowid=handle_rowid,
            chat_rowid=chat_rowid,
            text=None,
            is_from_me=True,
            when=when,
        )
        return self.add_message(
            handle_rowid=handle_rowid,
            chat_rowid=None,
            text=text,
            is_from_me=False,
            when=when,
        )


class FixtureRunner(ScriptRunner):
    """A ScriptRunner faked at the subprocess seam, with a REAL store behind it.

    ``sqlite3`` argv executes the exact production query against the fixture
    chat.db (read-only, JSON rows shaped like the CLI's ``-json`` output — blank
    stdout for an empty result set, matching the real binary). ``osascript`` argv
    is recorded and answered from a queue (default: one repeating success), so the
    send path is asserted at the argv boundary without a Mac.
    """

    def __init__(self, *osascript_results: ScriptResult) -> None:
        super().__init__()
        self.jxa_calls: list[tuple[str, ...]] = []
        self._jxa_results = list(osascript_results) or [
            ScriptResult("sent\n", "", 0)
        ]

    async def run(
        self, argv: Any, *, stdin: bytes | None = None
    ) -> ScriptResult:
        argv = tuple(argv)
        if argv[0] == self.sqlite3_path:
            assert argv[1] == "-readonly" and argv[2] == "-json"
            return self._execute(argv[3], argv[4])
        if argv[0] == self.osascript_path:
            self.jxa_calls.append(argv)
            if len(self._jxa_results) > 1:
                return self._jxa_results.pop(0)
            return self._jxa_results[0]
        raise AssertionError(f"unexpected binary {argv[0]!r}")

    @staticmethod
    def _execute(db_path: str, query: str) -> ScriptResult:
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                rows = [dict(row) for row in conn.execute(query)]
        except sqlite3.Error as exc:
            return ScriptResult("", str(exc), 1)
        return ScriptResult(json.dumps(rows) if rows else "", "", 0)


class FakeEngine:
    """Records the Engine calls the adapter makes (the model boundary fake)."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, str]] = []
        self.dispatched_guests: list[tuple[str, str, str | None]] = []
        self.cancelled: list[str] = []

    async def dispatch(
        self,
        *,
        thread_key: str,
        text: str,
        attachments: tuple[Attachment, ...] = (),
        is_general: bool = False,
        surface: Surface = Surface.DM,
    ) -> None:
        self.dispatched.append((thread_key, text))

    async def dispatch_guest(
        self,
        *,
        thread_key: str,
        text: str,
        from_label: str | None = None,
        surface: Surface = Surface.DM,
    ) -> None:
        self.dispatched_guests.append((thread_key, text, from_label))

    async def observe(
        self, *, thread_key: str, text: str, sender_name: str | None = None
    ) -> None:  # pragma: no cover - the adapter never observes (DMs only)
        raise AssertionError("iMessage v1 never observes ambient traffic")

    async def cancel(self, thread_key: str) -> bool:
        self.cancelled.append(thread_key)
        return True

    async def close(self, thread_key: str) -> str:
        return "Closed."

    async def rename(self, thread_key: str, title: str) -> str:
        return f"Renamed to {title}."

    async def active_tasks(self) -> list[Task]:
        return []

    async def branch(self, thread_key: str, title: str) -> str:
        return f"{thread_key}:branched"

    async def escalate(self, thread_key: str) -> str:
        return "escalated"

    async def revert(self, thread_key: str) -> str:
        return "reverted"

    async def route(self, thread_key: str, category: str) -> str:
        return f"routed {category}"

    async def downgrade_live_sessions(self) -> None:
        pass

    async def composed_skills(self) -> list[str]:
        return []

    async def list_watches(self) -> list[Watch]:
        return []

    async def cancel_watch(self, watch_id: int) -> str:
        return f"Cancelled #{watch_id}."
