"""iMessage adapter: a dumb pipe over the local Messages store, macOS-only.

Registration is darwin-gated in app wiring; the module is platform-neutral so
tests drive it with a fake chat.db and send runner. Inbound is a poll loop with
a persisted rowid cursor advanced at read, each row onto its thread's FIFO
worker, turns at most once (docs/LIFECYCLE.md).

Two postures, per config ``imessage.mode``. ``self`` (default): chief shares
the owner's Apple ID, so the owner's texts to it are ``is_from_me = 1`` rows in
the self-chat, and four mechanisms compensate — the self-chat query scope and
twin-row dedup (``imessage_store``), the BOT_PREFIX stamped on replies and
filtered on the way back, the out-of-band send guard (``imessage_send``).
``dedicated``: chief holds its own Apple ID in its own user session, the owner
is an ordinary correspondent, and all four switch off (the guard, in
``app.py``; see :meth:`IMessageAdapter._map` for delivery). The scope is
turned *off*, never repointed at the owner's handle — that chat is now chief's
real conversation with them, so scoping it would poll chief's own replies back
as owner input.
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
        dedicated: bool = False,
    ) -> None:
        self._on_message = on_message
        self._db_path = db_path
        self._cursor_store = RowCursor(cursor_path)
        self._owner_handles = frozenset(owner_handles)
        self._dedicated = dedicated
        self._poll_seconds = poll_seconds
        self._run_jxa = run_jxa
        self._restart = restart
        # Drained at poll stage, ahead of the FIFO worker: a gated turn suspends
        # its worker awaiting the answer, so the answer must bypass the worker
        # or it deadlocks that thread until the card times out.
        self._resolve_approval = resolve_approval
        self._cursor = 0
        self._dedup = RecentDedup()
        self._task: asyncio.Task[None] | None = None
        # One FIFO queue + worker per thread: parallel across, ordered within.
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
        if not self._dedicated and thread_key in self._owner_handles:
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

        The cursor is persisted at READ, before the turn runs: a crash drops
        that row rather than answering it twice (at-most-once); graceful
        self-edit restarts drain instead. Dedicated mode polls with an EMPTY
        scope, so the self-chat clause matches nothing and only real inbound
        rows qualify."""
        scope = frozenset() if self._dedicated else self._owner_handles
        rows = await asyncio.to_thread(
            fetch_rows, self._db_path, scope, self._cursor
        )
        for rowid, sender, text, from_me, group_chat, in_self, date in rows:
            self._cursor = rowid
            self._cursor_store.save(rowid)
            message = self._map(sender, text, from_me, group_chat, in_self)
            if message is None:
                continue
            if not self._dedicated and self._dedup.is_duplicate(
                (message.thread_key, message.sender, message.text), date
            ):
                continue  # twin rows are a self-DM artefact only
            if self._resolve_approval is not None and self._resolve_approval(message):
                continue  # answered a pending card — bypass the FIFO worker
            self._enqueue(message)

    def _enqueue(self, message: Message) -> None:
        """Route to the thread's FIFO queue; first sighting spawns its worker."""
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
        if text.startswith(BOT_PREFIX) and not self._dedicated:
            return None  # chief's own reply echoing back through the store
        if from_me and not in_self:
            return None  # owner->friend sent copy: not the self-chat
        if group_chat is not None:
            # Raw sender, never "owner", even for an owner handle: that would
            # take the dispatcher's owner path, replying to a group thread_key
            # the one-to-one send path cannot address — and would let anyone in
            # the group present as the owner (docs/SECURITY.md).
            return Message(
                channel=self.name, sender=sender, thread_key=group_chat, text=text
            )
        mapped = "owner" if (in_self or sender in self._owner_handles) else sender
        return Message(channel=self.name, sender=mapped, thread_key=sender, text=text)

