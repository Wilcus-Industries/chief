"""Web channel adapter: outbound frames broadcast to every open SSE client."""

import asyncio
from typing import Any

from chief.adapters.base import Adapter

Frame = dict[str, Any]


class WebAdapter(Adapter):
    """Bridges dispatcher output to the browser over server-sent events."""

    name = "web"

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[Frame]] = set()

    async def start(self) -> None:
        """The HTTP server owns the socket; the adapter itself needs no setup."""

    async def stop(self) -> None:
        for queue in self._queues:
            queue.put_nowait({"type": "closed"})

    async def send(self, thread_key: str, text: str) -> None:
        self._broadcast({"type": "final", "thread": thread_key, "text": text})

    async def send_delta(self, thread_key: str, text: str) -> None:
        self._broadcast({"type": "delta", "thread": thread_key, "text": text})

    def listen(self) -> asyncio.Queue[Frame]:
        """Register a new SSE client; pair with drop() when it disconnects."""
        queue: asyncio.Queue[Frame] = asyncio.Queue()
        self._queues.add(queue)
        return queue

    def drop(self, queue: asyncio.Queue[Frame]) -> None:
        self._queues.discard(queue)

    def _broadcast(self, frame: Frame) -> None:
        for queue in self._queues:
            queue.put_nowait(frame)
