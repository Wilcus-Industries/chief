"""The observer hub: fan-out of activity frames to dashboard SSE clients.

A core seam decoupled from origin channels and from the monitor EventBus.
Each connected client subscribes to at most one thread (its focused buffer).
Coarse ``tick`` frames broadcast to every client (the sidebar); rich frames
(``inbound``/``delta``/``tool``/``final``) target only the clients watching
that exact thread. Web clients subscribe via ``listen(thread)`` and read
frames off the returned queue.
"""

import asyncio
from typing import Any

Frame = dict[str, Any]


class ObserverHub:
    """In-process broadcast of activity frames to connected observers."""

    def __init__(self) -> None:
        self._watchers: dict[asyncio.Queue[Frame], str | None] = {}

    def listen(self, thread: str | None = None) -> asyncio.Queue[Frame]:
        """Register a new observer watching ``thread`` (None = coarse only);
        pair with drop() on disconnect."""
        queue: asyncio.Queue[Frame] = asyncio.Queue()
        self._watchers[queue] = thread
        return queue

    def drop(self, queue: asyncio.Queue[Frame]) -> None:
        self._watchers.pop(queue, None)

    def broadcast(self, frame: Frame) -> None:
        """Deliver a frame to every connected observer (coarse reach)."""
        for queue in self._watchers:
            queue.put_nowait(frame)

    def is_watched(self, thread: str) -> bool:
        """True if any connected client has ``thread`` as its focused buffer."""
        # ponytail: O(n) scan over connected clients; fine at LAN single-owner scale
        return thread in self._watchers.values()

    def to_watchers(self, thread: str, frame: Frame) -> None:
        """Deliver a rich frame only to the clients watching ``thread``."""
        for queue, watched in self._watchers.items():
            if watched == thread:
                queue.put_nowait(frame)

    def tick(self, thread_key: str, channel: str, preview: str) -> None:
        """One coarse activity tick for a completed turn on a tapped-off thread."""
        self.broadcast(
            {"type": "tick", "thread": thread_key,
             "channel": channel, "preview": preview}
        )

    def close(self) -> None:
        """Signal every observer to end its stream (daemon shutdown)."""
        for queue in self._watchers:
            queue.put_nowait({"type": "closed"})
