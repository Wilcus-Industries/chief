"""iMessage adapter: a dumb pipe over the local Messages store, macOS-only.

Registration is guarded by ``sys.platform == "darwin"`` in app wiring; the
module itself is platform-neutral so tests drive it anywhere with a fake
chat.db and a fake send runner.

Inbound is a poll loop over ``chat.db`` (read-only sqlite) with a persisted
rowid cursor — restarts neither replay old texts nor drop ones that arrived
while down. Self-DM posture: handles in ``owner_handles`` map to sender
``owner``; every other sender is delivered as-is (the dispatcher logs it as
a stranger and publishes it for monitors — notify tiers are agent policy,
built with monitors by the build-imessage package, never code here).
Replies to owner handles carry BOT_PREFIX: the received copy of chief's own
send re-enters the store as an inbound row, and the prefix is the echo
filter that keeps it from dispatching. Outbound goes through Messages via
a fixed JXA script; handle and text travel as argv, never spliced in.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from chief.adapters.base import Adapter, Message

logger = logging.getLogger(__name__)

BOT_PREFIX = "\U0001f916 "  # 🤖 — marks chief's replies in the shared self-chat

RunJxa = Callable[[str, tuple[str, ...]], Awaitable[str]]

SEND_TEXT_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Messages');\n"
    "  const matches = app.participants.whose({handle: argv[0]})();\n"
    "  let target = matches.length > 0 ? matches[0] : null;\n"
    "  if (target === null) {\n"
    "    const account = app.accounts.whose({serviceType: 'iMessage'})()[0];\n"
    "    target = account.participants.byId('iMessage;-;' + argv[0]);\n"
    "  }\n"
    "  app.send(argv[1], {to: target});\n"
    "  return 'sent';\n"
    "}"
)

# New, real, direct texts from other people past the cursor: is_from_me = 0
# keeps chief's sent copies out, associated_message_type = 0 drops tapbacks
# and edits, and the chat join skips group rows wholesale. Parameters are
# int-bound — no message text ever reaches this SQL.
POLL_QUERY = (
    "SELECT message.ROWID AS rowid, handle.id AS sender, message.text AS text, "
    "MAX(CASE WHEN chat.style IS NOT NULL AND chat.style != 45 "
    "THEN 1 ELSE 0 END) AS in_group, "
    "MAX(CASE WHEN chat.room_name IS NOT NULL THEN 1 ELSE 0 END) AS has_room "
    "FROM message JOIN handle ON message.handle_id = handle.ROWID "
    "LEFT JOIN chat_message_join ON chat_message_join.message_id = message.ROWID "
    "LEFT JOIN chat ON chat.ROWID = chat_message_join.chat_id "
    "WHERE message.ROWID > ? AND message.is_from_me = 0 "
    "AND message.associated_message_type = 0 "
    "AND message.text IS NOT NULL AND message.text != '' "
    "GROUP BY message.ROWID ORDER BY message.ROWID ASC LIMIT ?"
)
HEAD_QUERY = "SELECT COALESCE(MAX(ROWID), 0) FROM message"
POLL_BATCH_LIMIT = 200


async def _run_jxa_subprocess(script: str, argv: tuple[str, ...]) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-l", "JavaScript", "-e", script, *argv,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"osascript failed: {stderr.decode().strip()}")
    return stdout.decode().strip()


class IMessageAdapter(Adapter):
    """Polls the Messages store inbound; sends via OS automation outbound."""

    name = "imessage"

    def __init__(
        self,
        on_message: Callable[[Message], Awaitable[None]],
        *,
        db_path: Path,
        cursor_path: Path,
        owner_handles: tuple[str, ...],
        poll_seconds: float = 2.0,
        run_jxa: RunJxa = _run_jxa_subprocess,
    ) -> None:
        self._on_message = on_message
        self._db_path = db_path
        self._cursor_path = cursor_path
        self._owner_handles = frozenset(owner_handles)
        self._poll_seconds = poll_seconds
        self._run_jxa = run_jxa
        self._cursor = 0
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._cursor = self._load_cursor()
        if self._cursor == 0:
            # First boot: start at the store's head — no history replay.
            self._cursor = await asyncio.to_thread(self._head)
            self._save_cursor()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def send(self, thread_key: str, text: str) -> None:
        if thread_key in self._owner_handles:
            text = BOT_PREFIX + text
        await self._run_jxa(SEND_TEXT_SCRIPT, (thread_key, text))

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                logger.exception("imessage poll tick failed")
            await asyncio.sleep(self._poll_seconds)

    async def poll_once(self) -> None:
        """One poll tick: deliver new rows, advance the cursor."""
        rows = await asyncio.to_thread(self._fetch, self._cursor)
        for rowid, sender, text, in_group, has_room in rows:
            self._cursor = rowid
            if in_group or has_room:
                continue
            if text.startswith(BOT_PREFIX):
                continue  # chief's own reply echoing back through the store
            mapped = "owner" if sender in self._owner_handles else sender
            await self._on_message(
                Message(
                    channel=self.name,
                    sender=mapped,
                    thread_key=sender,
                    text=text,
                )
            )
        if rows:
            self._save_cursor()

    def _fetch(self, after: int) -> list[tuple[int, str, str, int, int]]:
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            cur = conn.execute(POLL_QUERY, (after, POLL_BATCH_LIMIT))
            return [
                (int(r[0]), str(r[1]), str(r[2]), int(r[3]), int(r[4]))
                for r in cur.fetchall()
            ]
        finally:
            conn.close()

    def _head(self) -> int:
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            return int(conn.execute(HEAD_QUERY).fetchone()[0])
        finally:
            conn.close()

    def _load_cursor(self) -> int:
        if self._cursor_path.exists():
            return int(self._cursor_path.read_text().strip() or 0)
        return 0

    def _save_cursor(self) -> None:
        self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self._cursor_path.write_text(str(self._cursor))
