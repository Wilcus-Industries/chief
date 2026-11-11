"""Per-thread FIFO delivery for the iMessage adapter.

One queue and one worker per thread: parallel across threads, strictly ordered
within one. Split out of ``imessage.py`` to keep that file under the length
cap; the poll loop only ever calls :meth:`ThreadFifo.put`, so a slow or hung
turn can never stall polling.

A message is recorded in flight (``track``) as it is enqueued: its cursor is
already saved, so the record is all that lets the next boot replay it if this
process dies with it still queued. The worker hands the record to ``handle``,
which ends it once the message is answered.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from chief.adapters.base import Message
from chief.agent.restart_gate import NullTicket, TurnTicket
from chief.selfedit.recovery import RestartBoundary

logger = logging.getLogger(__name__)

HandleTicketed = Callable[[Message, TurnTicket], Awaitable[None]]
Track = Callable[[Message], TurnTicket]
_Queue = asyncio.Queue[tuple[Message, TurnTicket]]


class ThreadFifo:
    """Routes messages onto their thread's worker; first sighting spawns it."""

    def __init__(
        self,
        handle: HandleTicketed,
        restart: RestartBoundary | None = None,
        track: Track | None = None,
    ) -> None:
        self._handle = handle
        self._restart = restart
        self._track = track
        self._queues: dict[str, _Queue] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}

    def put(self, message: Message, ticket: TurnTicket | None = None) -> None:
        """Enqueue; ``ticket`` is the record of a message coming back from a
        running turn's inbox, else a fresh one is made."""
        queue = self._queues.get(message.thread_key)
        if queue is None:
            queue = _Queue()
            self._queues[message.thread_key] = queue
            self._workers[message.thread_key] = asyncio.create_task(
                self._worker(message.thread_key, queue)
            )
        if ticket is None:
            ticket = self._track(message) if self._track else NullTicket()
        queue.put_nowait((message, ticket))

    async def _worker(
        self, thread_key: str, queue: _Queue
    ) -> None:
        """Drain one thread's queue serially, firing any pending self-edit
        restart after each turn commits. The cursor is already durable (saved
        at read), so the restart can never re-deliver an unanswered row."""
        while True:
            message, ticket = await queue.get()
            try:
                await self._handle(message, ticket)
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

    async def stop(self) -> None:
        for task in self._workers.values():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        self._queues.clear()
