"""The web UI's client-plane attachment (#153): one socket client, many browsers.

The chat surface behaves like another client-plane client — this module is that
client. :class:`SocketBridge` dials the daemon's own unix socket (in-process loopback)
over the reusable :class:`~chief.cli.connection.SocketConnection` seam and speaks the
:mod:`chief.client_plane.protocol` frame vocabulary verbatim; nothing web-side ever
touches the engine directly for chat. Every broadcast frame fans out to the open SSE
subscriptions, request/response exchanges (`list_threads` → `threads`, `switch` →
`backfill`, `status` → `status_snapshot`) are serialized on one in-flight slot, and
pending approval cards are tracked off the ``card``/``card_resolved`` stream so a page
load can render what is currently answerable — a card raised on ANY platform reaches
here via the mirror broadcast (#133/#136), which is exactly the cross-surface parity
the PRD requires.
"""

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from types import TracebackType
from typing import Final

from ..cli.connection import SocketConnection
from ..client_plane import (
    TYPE_BACKFILL,
    TYPE_CARD,
    TYPE_CARD_RESOLVED,
    TYPE_ERROR,
    TYPE_HELLO,
    TYPE_STATUS_SNAPSHOT,
    TYPE_THREADS,
    answer_frame,
    command_frame,
    inject_frame,
    list_threads_frame,
    status_frame,
    switch_frame,
    user_frame,
)

logger = logging.getLogger(__name__)

#: Per-subscriber fan-out queue bound. A browser that stops reading its SSE stream
#: sheds oldest frames rather than growing without bound.
QUEUE_SIZE: Final[int] = 256

#: How long / how often to re-dial while the daemon's socket listener comes up —
#: the web server and the socket server start under one gather, so the first dial
#: can race the bind.
_CONNECT_ATTEMPTS: Final[int] = 40
_CONNECT_DELAY: Final[float] = 0.25

_REQUEST_TIMEOUT: Final[float] = 10.0


class BridgeError(Exception):
    """A request the daemon answered with an ``error`` frame."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Subscription:
    """One SSE consumer's queue; a context manager that detaches on exit."""

    def __init__(self, bridge: "SocketBridge") -> None:
        self._bridge = bridge
        self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(QUEUE_SIZE)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._bridge._subscribers.discard(self)

    def push(self, frame: dict[str, object]) -> None:
        """Enqueue without blocking; a full queue drops its oldest frame first."""
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        self.queue.put_nowait(frame)


class SocketBridge:
    """The daemon-side web client: connect once, serve every browser from it."""

    def __init__(self, socket_path: str) -> None:
        self._path = socket_path
        self._connection = SocketConnection(socket_path)
        self._reader_task: asyncio.Task[None] | None = None
        self._subscribers: set[Subscription] = set()
        #: Pending approval cards by id — fed by the card/card_resolved stream. A
        #: replayed card (daemon re-arm) lands here too, so a browser attaching
        #: later still sees it.
        self.cards: dict[int, dict[str, object]] = {}
        #: The one in-flight request: ``(expected frame type, its future)``.
        self._pending: tuple[str, asyncio.Future[dict[str, object]]] | None = None
        self._request_lock = asyncio.Lock()
        self.connected = asyncio.Event()

    async def start(self) -> None:
        """Dial the daemon socket (retrying while it binds) and start the pump."""
        last: Exception | None = None
        for _ in range(_CONNECT_ATTEMPTS):
            try:
                await self._connection.connect()
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                last = exc
                await asyncio.sleep(_CONNECT_DELAY)
        else:
            raise ConnectionError(
                f"web bridge could not reach the client-plane socket at {self._path}"
            ) from last
        self._reader_task = asyncio.create_task(self._pump())
        self.connected.set()

    async def stop(self) -> None:
        """Tear the pump and the connection down; idempotent."""
        self.connected.clear()
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._connection.close()

    async def _pump(self) -> None:
        """Route every inbound frame: resolve the pending request, else fan out."""
        try:
            async for frame in self._connection.frames():
                self._on_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("web bridge frame pump failed")
        finally:
            self.connected.clear()

    def _on_frame(self, frame: dict[str, object]) -> None:
        ftype = frame.get("type")
        if ftype == TYPE_HELLO:
            return  # the greeting is transport-level; nothing to render or resolve
        if ftype == TYPE_CARD:
            approval_id = frame.get("approval_id")
            if isinstance(approval_id, int):
                self.cards[approval_id] = frame
        elif ftype == TYPE_CARD_RESOLVED:
            approval_id = frame.get("approval_id")
            if isinstance(approval_id, int):
                self.cards.pop(approval_id, None)
        if self._pending is not None:
            expect, future = self._pending
            if ftype in (expect, TYPE_ERROR) and not future.done():
                future.set_result(frame)
                self._pending = None
                return  # a direct response is consumed, never fanned out
        for subscriber in list(self._subscribers):
            subscriber.push(frame)

    def subscribe(self) -> Subscription:
        """Attach one SSE consumer to the broadcast fan-out."""
        subscription = Subscription(self)
        self._subscribers.add(subscription)
        return subscription

    def pending_cards(self) -> Iterator[dict[str, object]]:
        """The currently-answerable approval cards, oldest first."""
        return iter(sorted(self.cards.values(), key=lambda c: int(c["approval_id"])))  # type: ignore[call-overload]

    async def send(self, frame: dict[str, object]) -> None:
        """Write one frame to the daemon (fire-and-forget: replies stream back)."""
        await self._connection.send(frame)

    async def request(
        self,
        frame: dict[str, object],
        *,
        expect: str,
        timeout: float = _REQUEST_TIMEOUT,
    ) -> dict[str, object]:
        """Send ``frame`` and await the matching response (or ``error``) frame.

        Serialized on one lock: the socket protocol has no request ids, so exactly
        one exchange may be in flight — fine for a single-owner surface. Raises
        :class:`BridgeError` when the daemon answers with an ``error`` frame.
        """
        async with self._request_lock:
            future: asyncio.Future[dict[str, object]] = (
                asyncio.get_running_loop().create_future()
            )
            self._pending = (expect, future)
            try:
                await self.send(frame)
                response = await asyncio.wait_for(future, timeout)
            finally:
                self._pending = None
        if response.get("type") == TYPE_ERROR:
            raise BridgeError(str(response.get("code")), str(response.get("message")))
        return response

    async def threads(self) -> list[dict[str, object]]:
        """Every platform's live threads, via ``list_threads`` (#134)."""
        response = await self.request(list_threads_frame(), expect=TYPE_THREADS)
        threads = response.get("threads")
        return [t for t in threads if isinstance(t, dict)] if isinstance(
            threads, list
        ) else []

    async def backfill(
        self, platform: str, thread_key: str
    ) -> list[dict[str, object]]:
        """One thread's recent history, via ``switch`` → ``backfill`` (#134)."""
        response = await self.request(
            switch_frame(platform, thread_key), expect=TYPE_BACKFILL
        )
        messages = response.get("messages")
        return [m for m in messages if isinstance(m, dict)] if isinstance(
            messages, list
        ) else []

    async def status(self) -> dict[str, object]:
        """The daemon's point-in-time snapshot, via ``status`` (#138)."""
        return await self.request(status_frame(), expect=TYPE_STATUS_SNAPSHOT)

    async def send_user(self, thread_key: str, text: str) -> None:
        """Dispatch an owner turn on the web's own (cli-platform) thread."""
        await self.send(user_frame(thread_key, text))

    async def send_command(self, thread_key: str, name: str, arg: str = "") -> None:
        """Run an owner slash-command; its reply streams back on the socket."""
        await self.send(command_frame(thread_key, name, arg))

    async def send_inject(self, platform: str, thread_key: str, text: str) -> None:
        """Drive a FOREIGN platform's thread with an owner turn (#135)."""
        await self.send(inject_frame(platform, thread_key, text))

    async def answer(self, approval_id: int, action: str) -> None:
        """Answer an approval card; resolution arrives as ``card_resolved``."""
        await self.send(answer_frame(approval_id, action))
