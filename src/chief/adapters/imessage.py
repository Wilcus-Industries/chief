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
from chief.adapters.imessage_cursor import RowCursor, Store
from chief.adapters.imessage_fifo import ThreadFifo
from chief.adapters.imessage_send import (
    SEND_TEXT_SCRIPT,
    RunJxa,
    run_jxa_subprocess,
)
from chief.adapters.imessage_store import RecentDedup
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
        owner_db_path: Path | None = None,
        self_handles: tuple[str, ...] = (),
    ) -> None:
        self._on_message = on_message
        self._owner_handles = frozenset(owner_handles)
        self._self_handles = frozenset(self_handles)
        self._dedicated = dedicated
        self._poll_seconds = poll_seconds
        self._run_jxa = run_jxa
        # Drained at poll stage, ahead of the FIFO worker: a gated turn suspends
        # its worker awaiting the answer, so the answer must bypass the worker
        # or it deadlocks that thread until the card times out.
        self._resolve_approval = resolve_approval
        self._dedup = RecentDedup()
        self._task: asyncio.Task[None] | None = None
        self._fifo = ThreadFifo(on_message, restart)
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
                message = self._map(sender, text, from_me, group_chat, in_self)
                if message is None:
                    continue
                if not self._dedicated and self._dedup.is_duplicate(
                    (message.thread_key, message.sender, message.text), date
                ):
                    continue  # twin rows are a self-DM artefact only
                if self._resolve_approval is not None and self._resolve_approval(
                    message
                ):
                    continue  # answered a pending card — bypass the FIFO worker
                self._fifo.put(message)

    async def drain(self) -> None:
        """Block until every per-thread queue is empty (test seam)."""
        await self._fifo.drain()

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

