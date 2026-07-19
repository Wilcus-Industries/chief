# styleguide: file-length — self-DM posture, twin dedup, and per-thread FIFO
# dispatch (queues/workers) cohere here; splitting the adapter reads worse.
"""iMessage adapter: a dumb pipe over the local Messages store, macOS-only.

Registration is guarded by ``sys.platform == "darwin"`` in app wiring; the
module itself is platform-neutral so tests drive it anywhere with a fake
chat.db and a fake send runner.

Inbound is a poll loop over ``chat.db`` (read-only sqlite) with a persisted
rowid cursor advanced at read: each row is dispatched onto its thread's FIFO
worker so a slow turn on one thread never stalls another, and turns run at
most once (a hard crash mid-turn drops that row rather than replaying it;
graceful self-edit restarts drain in-flight turns first). Same-account
self-DM posture: chief runs on the owner's own Apple
ID, so self-chat texts carry ``is_from_me = 1``. A row is delivered when it is
a real inbound (``is_from_me = 0``) OR sits in the owner's self-chat (scoped by
``chat.chat_identifier IN owner_handles``, keeping other conversations out);
self-chat rows map to sender ``owner``, strangers pass through as-is. Replies
to owner handles carry BOT_PREFIX: chief's own send re-enters as an
``is_from_me = 1`` self-chat row, the prefix its sole echo filter. macOS also
records one self-DM as several twin rows (same text); RecentDedup collapses
them to one turn. Outbound goes through Messages via a fixed JXA script; handle
and text travel as argv, never spliced in.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from chief.adapters.base import Adapter, Message
from chief.adapters.imessage_store import (
    HEAD_QUERY,
    POLL_BATCH_LIMIT,
    POLL_QUERY,
    RecentDedup,
    text_of,
)
from chief.selfedit.recovery import RestartBoundary

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
        restart: RestartBoundary | None = None,
        resolve_approval: Callable[[Message], bool] | None = None,
    ) -> None:
        self._on_message = on_message
        self._db_path = db_path
        self._cursor_path = cursor_path
        self._owner_handles = frozenset(owner_handles)
        self._poll_seconds = poll_seconds
        self._run_jxa = run_jxa
        self._restart = restart
        # Drain an approval answer at the poll stage, ahead of the per-thread
        # FIFO worker: a gated turn suspends its worker awaiting the owner's
        # answer, so that answer must bypass the worker or it deadlocks the
        # whole thread until the card times out (fail-closed deny).
        self._resolve_approval = resolve_approval
        self._cursor = 0
        self._dedup = RecentDedup()
        self._task: asyncio.Task[None] | None = None
        # One FIFO queue + worker per thread: a slow turn on one thread can't
        # stall another's, while same-thread turns stay serialized in order.
        self._queues: dict[str, asyncio.Queue[Message]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        self._cursor = self._load_cursor()
        if self._cursor == 0:
            # First boot: start at the store's head — no history replay.
            self._cursor = await asyncio.to_thread(self._head)
            self._save_cursor()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        for task in [self._task, *self._workers.values()]:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._workers.clear()
        self._queues.clear()

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
        """One poll tick: enqueue new rows onto their thread's worker, never
        awaiting a turn — so one slow/hung turn can't stall the poll loop.

        The cursor advances and is persisted at READ (before the turn runs), so
        the row can't be re-polled and answered twice (at-most-once): a hard
        crash between enqueue and reply drops that row rather than replaying it;
        graceful self-edit restarts still drain in-flight turns first."""
        rows = await asyncio.to_thread(self._fetch, self._cursor)
        for rowid, sender, text, from_me, in_group, has_room, in_self, date in rows:
            self._cursor = rowid
            self._save_cursor()
            message = self._map(sender, text, from_me, in_group, has_room, in_self)
            if message is None or self._dedup.is_duplicate(
                (message.sender, message.text), date
            ):
                continue
            if self._resolve_approval is not None and self._resolve_approval(message):
                continue  # answered a pending card — bypass the FIFO worker
            self._enqueue(message)

    def _enqueue(self, message: Message) -> None:
        """Route a message to its thread's FIFO queue, spawning a worker the
        first time that thread is seen."""
        queue = self._queues.get(message.thread_key)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[message.thread_key] = queue
            self._workers[message.thread_key] = asyncio.create_task(
                self._worker(message.thread_key, queue)
            )
        queue.put_nowait(message)

    async def _worker(
        self, thread_key: str, queue: "asyncio.Queue[Message]"
    ) -> None:
        """Drain one thread's queue serially (FIFO), firing any pending self-edit
        restart after each turn commits. The cursor is already durable (saved at
        read), so the restart can never re-deliver an unanswered row."""
        while True:
            message = await queue.get()
            try:
                await self._on_message(message)
                if self._restart is not None:
                    await self._restart.fire_if_requested()
            except Exception:
                logger.exception("imessage turn failed for %s", thread_key)
            finally:
                queue.task_done()

    async def drain(self) -> None:
        """Block until every per-thread queue is empty (test seam)."""
        for queue in list(self._queues.values()):
            await queue.join()

    def _map(
        self,
        sender: str,
        text: str,
        from_me: int,
        in_group: int,
        has_room: int,
        in_self: int,
    ) -> Message | None:
        """Turn a polled row into a deliverable Message, or None to skip it."""
        if in_group or has_room:
            return None
        if not text:
            return None  # attachment-only row: attributedBody held no text
        if text.startswith(BOT_PREFIX):
            return None  # chief's own reply echoing back through the store
        if from_me and not in_self:
            return None  # owner->friend sent copy: not the self-chat
        mapped = "owner" if (in_self or sender in self._owner_handles) else sender
        return Message(
            channel=self.name, sender=mapped, thread_key=sender, text=text
        )

    def _fetch(
        self, after: int
    ) -> list[tuple[int, str, str, int, int, int, int, int]]:
        handles = tuple(self._owner_handles)
        scope = ",".join("?" for _ in handles) if handles else "NULL"
        query = POLL_QUERY.format(scope=scope)
        params: tuple[object, ...] = (*handles, after, POLL_BATCH_LIMIT)
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            cur = conn.execute(query, params)
            return [
                (
                    int(r[0]), str(r[1]), text_of(r[2], r[7]), int(r[3]),
                    int(r[4]), int(r[5]), int(r[6]), int(r[8]),
                )
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
