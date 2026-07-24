"""Web channel adapter: outbound frames broadcast to every open SSE client.

For ``web:`` threads the adapter is the origin channel — the dispatcher calls
``send``/``send_delta`` directly and the browser streams the turn. For every
*other* channel (iMessage) the adapter is a passive mirror: it subscribes to
the event bus and turns inbound peer messages and outbound replies into SSE
frames, so the cockpit can watch and drive a conversation that lives on another
channel. Bus events on the ``web`` channel are ignored — that path is already
covered by the direct sends, and mirroring it would double every frame.
"""

import asyncio
from typing import Any

from chief.adapters.base import Adapter
from chief.bus import Event, EventBus

Frame = dict[str, Any]


class WebAdapter(Adapter):
    """Bridges dispatcher output to the browser over server-sent events."""

    name = "web"

    def __init__(self, bus: EventBus | None = None) -> None:
        self._queues: set[asyncio.Queue[Frame]] = set()
        self._bus = bus
        self._unsubscribe: Any = None

    async def start(self) -> None:
        """The HTTP server owns the socket; the adapter only wires the mirror."""
        if self._bus is not None and self._unsubscribe is None:
            self._unsubscribe = self._bus.subscribe(self._on_event)

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for queue in self._queues:
            queue.put_nowait({"type": "closed"})

    async def send(self, thread_key: str, text: str) -> None:
        self._broadcast({"type": "final", "thread": thread_key, "text": text})

    async def send_delta(self, thread_key: str, text: str) -> None:
        self._broadcast({"type": "delta", "thread": thread_key, "text": text})

    async def _on_event(self, event: Event) -> None:
        """Mirror another channel's traffic to the browser as SSE frames.

        Peer inbound (a non-owner message) renders as its own role; owner
        inbound is skipped — the browser shows the owner's own text
        optimistically and re-emitting it would duplicate. Outbound is chief's
        reply, already the ``final`` shape.
        """
        if event.channel == self.name:
            return  # web threads use the direct send path above
        payload = event.payload
        thread = str(payload.get("thread_key", ""))
        if event.type == "message.inbound":
            if payload.get("sender") == "owner":
                return
            self._broadcast(
                {
                    "type": "peer",
                    "thread": thread,
                    "text": str(payload.get("text", "")),
                    "sender": str(payload.get("sender", "")),
                }
            )
        elif event.type == "message.outbound":
            self._broadcast(
                {
                    "type": "final",
                    "thread": thread,
                    "text": str(payload.get("text", "")),
                }
            )

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
