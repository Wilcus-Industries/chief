"""Web channel adapter: the origin channel for ``web:`` threads.

The dispatcher calls ``send``/``send_delta`` for a web-origin turn and the
adapter streams those ``final``/``delta`` frames to the browser through the
shared :class:`~chief.hub.ObserverHub` — targeting only the clients watching
that thread, so a multi-tab owner sees a web thread's deltas in one tab, not
all. Every *other* channel's turns are
observed via the dispatcher's coarse ``tick`` (see ``chief.dispatch``), not
mirrored here — so this adapter no longer touches the monitor EventBus.
"""

from chief.adapters.base import Adapter
from chief.hub import ObserverHub


class WebAdapter(Adapter):
    """Streams web-origin turn output to the browser via the observer hub."""

    name = "web"

    def __init__(self, hub: ObserverHub) -> None:
        self._hub = hub

    async def start(self) -> None:
        """The HTTP server owns the socket; queue lifecycle lives on the hub."""

    async def stop(self) -> None:
        """The hub is closed at daemon shutdown, not per-adapter."""

    async def send(self, thread_key: str, text: str) -> None:
        self._hub.to_watchers(
            thread_key, {"type": "final", "thread": thread_key, "text": text}
        )

    async def send_delta(self, thread_key: str, text: str) -> None:
        self._hub.to_watchers(
            thread_key, {"type": "delta", "thread": thread_key, "text": text}
        )
