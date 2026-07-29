"""Per-thread FIFO delivery for the iMessage adapter.

One queue and one worker per thread: parallel across threads, strictly ordered
within one. Split out of ``imessage.py`` to keep that file under the length
cap; the poll loop only ever calls :meth:`ThreadFifo.put`, so a slow or hung
turn can never stall polling.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from chief.adapters.base import Message
from chief.selfedit.recovery import RestartBoundary

logger = logging.getLogger(__name__)


class ThreadFifo:
    """Routes messages onto their thread's worker; first sighting spawns it."""

    def __init__(
        self,
        handle: Callable[[Message], Awaitable[None]],
        restart: RestartBoundary | None = None,
    ) -> None:
        self._handle = handle
        self._restart = restart
        self._queues: dict[str, asyncio.Queue[Message]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}

    def put(self, message: Message) -> None:
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
        """Drain one thread's queue serially, firing any pending self-edit
        restart after each turn commits. The cursor is already durable (saved
        at read), so the restart can never re-deliver an unanswered row."""
        while True:
            message = await queue.get()
            try:
                await self._handle(message)
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
