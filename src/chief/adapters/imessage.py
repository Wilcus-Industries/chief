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
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from chief.adapters.base import Adapter, Inject, Message
from chief.adapters.imessage_attachments import ImageStash
from chief.adapters.imessage_cursor import RowCursor, Store
from chief.adapters.imessage_fifo import HandleTicketed, ThreadFifo, Track
from chief.adapters.imessage_map import BOT_PREFIX, map_row
from chief.adapters.imessage_send import (
    SEND_TEXT_SCRIPT,
    RunJxa,
    run_jxa_subprocess,
)
from chief.adapters.imessage_store import RecentDedup
from chief.selfedit.recovery import RestartBoundary

logger = logging.getLogger(__name__)

__all__ = ["BOT_PREFIX", "IMessageAdapter"]


class IMessageAdapter(Adapter):
    """Polls the Messages store inbound; sends via OS automation outbound."""

    name = "imessage"

    def __init__(
        self,
        on_message: HandleTicketed,
        *,
        db_path: Path,
        cursor_path: Path,
        owner_handles: tuple[str, ...],
        poll_seconds: float = 2.0,
        run_jxa: RunJxa = run_jxa_subprocess,
        restart: RestartBoundary | None = None,
        resolve_approval: Callable[[Message], bool] | None = None,
        inject: Inject | None = None,
        dedicated: bool = False,
        owner_db_path: Path | None = None,
        self_handles: tuple[str, ...] = (),
        track: Track | None = None,
        stash: ImageStash | None = None,
    ) -> None:
        self._on_message = on_message
        self._owner_handles = frozenset(owner_handles)
        self._self_handles = frozenset(self_handles)
        self._dedicated = dedicated
        self._stash = stash  # the owner's photos, for a model to see
        self._poll_seconds = poll_seconds
        self._run_jxa = run_jxa
        # Drained at poll stage, ahead of the FIFO worker: a gated turn suspends
        # its worker awaiting the answer, so the answer must bypass the worker
        # or it deadlocks that thread until the card times out.
        self._resolve_approval = resolve_approval
        # Same bypass for texting's interrupt-by-default: an owner text landing
        # mid-turn goes to that turn, its leftover back through the FIFO.
        self._inject = inject
        self._dedup = RecentDedup()
        self._task: asyncio.Task[None] | None = None
        self._fifo = ThreadFifo(on_message, restart, track)
        self._stores = [Store(db_path, RowCursor(cursor_path))]
        if owner_db_path is not None and owner_db_path != db_path:
            self._stores.append(
                Store(
                    owner_db_path,
                    RowCursor(cursor_path.with_name(cursor_path.name + ".owner")),
                    mine=False,
                )
            )

    async def start(self) -> None:
        for store in list(self._stores):
            try:
                await asyncio.to_thread(store.prime)
            except Exception:
                if store.mine:
                    raise
                # The owner's store needs its own Full Disk Access grant and a
                # readable ~/Library/Messages (docs/OPERATIONS.md). Missing
                # either must cost chief that store, not the daemon — and with
                # it the web UI, which is the other half of the boot check.
                logger.exception("imessage: dropping unreadable %s", store.db_path)
                self._stores.remove(store)
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self._fifo.stop()

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
        for store in self._stores:
            rows = await asyncio.to_thread(store.fetch, scope)
            for rowid, sender, text, from_me, group_chat, in_self, date in rows:
                store.advance(rowid)
                if not store.mine and sender in self._self_handles:
                    # Chief's own reply, seen from the owner's side as an
                    # ordinary inbound row. Polling it would answer itself.
                    continue
                owner = bool(in_self) or sender in self._owner_handles
                images: tuple[str, ...] = ()
                if self._stash is not None:
                    text, images = await asyncio.to_thread(
                        self._stash.resolve, store.db_path, rowid, text, owner)
                message = map_row(self.name, sender, text, from_me, group_chat,
                                  in_self, date, self._owner_handles, self._dedicated)
                if message is None:
                    continue
                message = replace(message, attachments=images)
                if not self._dedicated and self._dedup.is_duplicate(
                    (message.thread_key, message.sender, message.text), message.at
                ):
                    continue  # twin rows are a self-DM artefact only
                if self._resolve_approval is not None and self._resolve_approval(
                    message
                ):
                    continue  # answered a pending card — bypass the FIFO worker
                if self._inject is not None and self._inject(message, self._fifo.put):
                    continue  # taken by the thread's running turn
                self._fifo.put(message)

    async def drain(self) -> None:
        """Block until every per-thread queue is empty (test seam)."""
        await self._fifo.drain()
