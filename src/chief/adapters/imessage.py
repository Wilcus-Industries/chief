"""iMessage adapter: a dumb pipe over the local Messages store, macOS-only.

Registration is guarded by ``sys.platform == "darwin"`` in app wiring; the
module itself is platform-neutral so tests drive it anywhere with a fake
chat.db and a fake send runner.

Inbound is a poll loop over ``chat.db`` with a persisted rowid cursor advanced
at read: each row is dispatched onto its thread's FIFO worker so a slow turn on
one thread never stalls another, and turns run at most once (a hard crash
mid-turn drops that row rather than replaying it; graceful self-edit restarts
drain in-flight turns first). Same-account self-DM posture: chief runs on the
owner's own Apple ID, so self-chat texts carry ``is_from_me = 1``. A row is
delivered when it is a real inbound (``is_from_me = 0``) OR sits in the owner's
self-chat; self-chat rows map to sender ``owner``, strangers pass through
as-is. Replies to owner handles carry BOT_PREFIX: chief's own send re-enters as
an ``is_from_me = 1`` self-chat row, the prefix its sole echo filter; twin-row
dedup and the query scope live in ``imessage_store``, the JXA send path and the
out-of-band send guard in ``imessage_send``.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from chief.adapters.base import Adapter, Message
from chief.adapters.imessage_send import (
    SEND_TEXT_SCRIPT,
    RunJxa,
    run_jxa_subprocess,
)
from chief.adapters.imessage_store import (
    RecentDedup,
    RowCursor,
    fetch_rows,
    head_rowid,
)
from chief.selfedit.recovery import RestartBoundary

logger = logging.getLogger(__name__)

BOT_PREFIX = "\U0001f916 "  # 🤖 — marks chief's replies in the shared self-chat


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
        run_jxa: RunJxa = run_jxa_subprocess,
        restart: RestartBoundary | None = None,
        resolve_approval: Callable[[Message], bool] | None = None,
    ) -> None:
        self._on_message = on_message
        self._db_path = db_path
        self._cursor_store = RowCursor(cursor_path)
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
        self._cursor = self._cursor_store.load()
        if self._cursor == 0:
            # First boot: start at the store's head — no history replay.
            self._cursor = await asyncio.to_thread(head_rowid, self._db_path)
            self._cursor_store.save(self._cursor)
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
        rows = await asyncio.to_thread(
            fetch_rows, self._db_path, self._owner_handles, self._cursor
        )
        for rowid, sender, text, from_me, group_chat, in_self, date in rows:
            self._cursor = rowid
            self._cursor_store.save(rowid)
            message = self._map(sender, text, from_me, group_chat, in_self)
            if message is None or self._dedup.is_duplicate(
                (message.thread_key, message.sender, message.text), date
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

    async def _worker(self, thread_key: str, queue: asyncio.Queue[Message]) -> None:
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
        self, sender: str, text: str, from_me: int,
        group_chat: str | None, in_self: int,
    ) -> Message | None:
        """Turn a polled row into a deliverable Message, or None to skip it.

        A group message threads on its chat, not on whoever spoke — the one
        case where ``sender`` and ``thread_key`` differ. Groups are never the
        owner's self-chat, so they take the stranger path: published for
        monitors, never answered. Which groups matter is monitor policy."""
        if not text:
            return None  # attachment-only row: attributedBody held no text
        if text.startswith(BOT_PREFIX):
            return None  # chief's own reply echoing back through the store
        if from_me and not in_self:
            return None  # owner->friend sent copy: not the self-chat
        if group_chat is not None:
            # Raw sender, never "owner", even for an owner handle: sender
            # "owner" takes the dispatcher's owner path, which runs a turn and
            # replies to thread_key — and a group thread_key is a chat the
            # one-to-one send path cannot address. It also means nobody in a
            # group can present as the owner; group text is uniformly
            # untrusted, and owner instructions arrive only via the self-chat.
            return Message(
                channel=self.name, sender=sender, thread_key=group_chat, text=text
            )
        mapped = "owner" if (in_self or sender in self._owner_handles) else sender
        return Message(channel=self.name, sender=mapped, thread_key=sender, text=text)

