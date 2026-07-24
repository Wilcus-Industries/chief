"""The observer hub: fan-out of activity frames to dashboard SSE clients.

A core seam decoupled from origin channels and from the monitor EventBus.
The dispatcher emits one coarse ``tick`` per completed turn; the web origin
adapter streams its own ``delta``/``final`` frames through the same hub. Web
clients subscribe via ``listen()`` and read frames off the returned queue.
"""

import asyncio
from typing import Any

Frame = dict[str, Any]


class ObserverHub:
    """In-process broadcast of activity frames to connected observers."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[Frame]] = set()

    def listen(self) -> asyncio.Queue[Frame]:
        """Register a new observer; pair with drop() on disconnect."""
        queue: asyncio.Queue[Frame] = asyncio.Queue()
        self._queues.add(queue)
        return queue

    def drop(self, queue: asyncio.Queue[Frame]) -> None:
        self._queues.discard(queue)

    def broadcast(self, frame: Frame) -> None:
        """Deliver a frame to every connected observer."""
        for queue in self._queues:
            queue.put_nowait(frame)

    def tick(self, thread_key: str, channel: str, preview: str) -> None:
        """One coarse activity tick for a completed turn on a tapped-off thread."""
        self.broadcast(
            {"type": "tick", "thread": thread_key,
             "channel": channel, "preview": preview}
        )

    def close(self) -> None:
        """Signal every observer to end its stream (daemon shutdown)."""
        for queue in self._queues:
            queue.put_nowait({"type": "closed"})
