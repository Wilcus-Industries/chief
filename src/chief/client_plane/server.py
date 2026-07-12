"""The always-on client-plane socket listener (#130, #131).

:class:`SocketServer` binds a unix-domain socket and serves LF-delimited JSON frames
(:mod:`chief.client_plane.protocol`) to any number of concurrent clients. It is a
standalone lifecycle object — ``run()``/``stop()`` mirror the adapter names so
``app.serve`` reads uniformly — and it stays **transport-only**: it owns no engine or
task manager, so the socket is unconditional infrastructure that stays up even on a
zero-platform boot. The CLI adapter (#131) injects an application via
:meth:`set_handler` (inbound dispatch) and pushes engine output out via
:meth:`broadcast`.

It owns one piece of cross-cutting state: :attr:`SocketServer.delivery_lock`, the
**delivery barrier** (#134). Attaching a client and emitting a frame both touch "is
anyone listening?" and "what is held in the log?", and those two answers must agree or
a frame is lost or delivered twice. The barrier is what makes them agree; every party
on both sides of that question (the CLI IO's emit, the mirror's, the connect hook's
claim, the switch backfill's read) takes it. See :attr:`SocketServer.delivery_lock`.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path

from .protocol import (
    TYPE_PING,
    FrameError,
    decode,
    encode,
    error_frame,
    hello_frame,
    pong_frame,
)

logger = logging.getLogger(__name__)

#: Writes one frame to a single connection (the one that sent the inbound frame). A
#: handler uses it for a direct reply — a command's answer, a validation error — while
#: turn output fans out to every client via :meth:`SocketServer.broadcast`. It **writes
#: without draining**; the server flushes once, after the hook or handler returns. See
#: :func:`_frame_sender`.
FrameSender = Callable[[Mapping[str, object]], Awaitable[None]]

#: An inbound-frame application: ``(frame, sender) -> handled``. Returns ``True`` when
#: it owns the frame (it has already answered through ``sender``), ``False`` to let the
#: server fall back to its ``unknown_type`` error. Transport frames (``ping``) never
#: reach it — the server answers those itself.
FrameHandler = Callable[
    [Mapping[str, object], FrameSender], Awaitable[bool]
]

#: Runs once per accepted connection, after the hello and before the connection joins
#: the broadcast set; the #132 CLI adapter replays held frames through it. Frames it
#: sends through its ``FrameSender`` reach only this connection and precede any live
#: traffic on it.
ConnectHook = Callable[[FrameSender], Awaitable[None]]

#: Socket file mode: owner read/write only. The socket is a local-control surface, so no
#: group/other access — mirrors the db_path fence class (config.SELF_CONFIG_DENYLIST).
_SOCKET_MODE = 0o600


def _frame_sender(writer: asyncio.StreamWriter) -> FrameSender:
    """Build one connection's :data:`FrameSender`: write the frame, never ``drain`` it.

    **Draining under :attr:`SocketServer.delivery_lock` is forbidden**, and this is
    where that is enforced. Both senders built here can run inside the barrier (the
    connect hook's claim-replay, and the switch backfill via ``_handle_switch``), and
    both can push megabytes: a replayed ``file`` frame is multi-MB base64, far past the
    transport's ~64 KB high-water mark. A client that has stopped reading — a Ctrl-Z'd
    terminal — would park such a ``drain()`` indefinitely, and parked *under the
    barrier* that one client freezes every emit, every chat-stack mirror and every other
    attach in the daemon until it dies. :meth:`SocketServer.broadcast` skips draining
    for the same reason; keep the two consistent and never reintroduce a drain here.

    Deferring the flush costs no ordering. Frames are queued on the stream in call
    order, and ``drain()`` only *waits* for that queue to shrink — it can neither
    reorder nor overtake — so replay-before-live and backfill-before-live still hold
    once the caller drains after releasing the barrier. Backpressure is not dropped,
    just moved: the server flushes when the hook or handler returns, which stalls only
    that connection.
    """

    async def sender(frame: Mapping[str, object]) -> None:
        writer.write(encode(frame))

    return sender


class SocketServer:
    """A unix-socket JSON-frame listener that survives bad input and multiple clients.

    Each accepted connection runs its own handler task: it is greeted with a hello
    frame, then answers ``ping`` with ``pong`` and any malformed or unknown frame with
    an error frame — a bad frame is reported, never fatal, so one client's garbage can
    neither crash the daemon nor drop another client. ``run()`` blocks serving until
    cancelled or ``stop()``; both remove the socket file so the next boot binds cleanly.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.started = asyncio.Event()
        self._stopped = asyncio.Event()
        self._closing = False
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        #: The delivery barrier (#134). Serializes "emit" against "attach" so that the
        #: invariant holds: **a frame that has been broadcast is already committed to
        #: the message log before any other party can claim or read the log.** An emit
        #: (snapshot :attr:`has_clients` → broadcast → record) and an attach (claim the
        #: held rows → join :attr:`_clients`) each run wholly inside it, so there are
        #: only two interleavings and both deliver exactly once: emit-first records the
        #: frame held *before* the joiner can claim (so the claim replays it), and
        #: attach-first joins the broadcast set *before* the emit's snapshot (so the
        #: frame goes out live). The switch backfill (#134) and the chat mirror (#133)
        #: take it too, so their log reads/writes cannot straddle a broadcast either.
        #: Lock order — always ``delivery_lock`` → ``MessageLog._lock``, never the
        #: reverse, so the two cannot deadlock. Nothing may block on *client
        #: backpressure* while holding it: a party that drains here hands one unread
        #: socket the power to freeze every other party (see :func:`_frame_sender`).
        self.delivery_lock = asyncio.Lock()
        #: Live client writers, for outbound push (#131). Registered/discarded in
        #: :meth:`_handle` alongside the handler task; :meth:`broadcast` snapshots it.
        self._clients: set[asyncio.StreamWriter] = set()
        #: The injected inbound application (#131). ``None`` ⇒ transport-only (#130):
        #: every non-ping frame falls through to the ``unknown_type`` error.
        self._handler: FrameHandler | None = None
        #: The injected per-connection connect hook (#132). ``None`` ⇒ no replay: a new
        #: connection registers for broadcast the instant after its hello, as in #131.
        self._connect_hook: ConnectHook | None = None

    @property
    def has_clients(self) -> bool:
        """True iff at least one live (not-closing) client is attached (#132).

        The CLI IO snapshots this *before* a broadcast to decide whether an outbound
        frame is delivered live or logged held for the next attach. That snapshot is
        only meaningful under :attr:`delivery_lock` (#134): an attach joins the client
        set inside the barrier, so reading this outside it can see a client that has
        not yet claim-replayed, or miss one that is about to.
        """
        return any(not w.is_closing() for w in self._clients)

    def set_handler(self, handler: FrameHandler | None) -> None:
        """Install (or clear, with ``None``) the inbound-frame application (#131).

        Before this is called — and after it is cleared with ``None`` on shutdown — the
        server is byte-identical to #130: pings pong, everything else errors
        ``unknown_type``. With a handler set, each decoded non-ping frame is offered to
        it first.
        """
        self._handler = handler

    def set_connect_hook(self, hook: ConnectHook | None) -> None:
        """Install (or clear, with ``None``) the per-connection connect hook (#132).

        With no hook — before this is called and after it is cleared on shutdown — a
        new connection registers for broadcast the instant after its hello, byte-for-
        byte as in #131. With a hook set, each accepted connection runs it (after the
        hello, before registration) so the #132 CLI adapter can replay held frames.
        """
        self._connect_hook = hook

    async def broadcast(self, frame: Mapping[str, object]) -> None:
        """Push ``frame`` to every live client — engine output fans out here (#131).

        Deliberately does **not** ``drain()``: it is awaited from inside the engine's
        turn loop (``_run_turn`` → ``CliTaskIO.send``), so blocking on a wedged client's
        backpressure would freeze the turn for every thread. A dead client is reaped by
        two other paths, not by this write: the ``is_closing()`` skip drops one already
        detaching, and its handler's ``readline`` hits EOF and discards it in the
        ``_handle`` finally. (A CPython ``StreamWriter.write`` to a broken transport
        does not raise synchronously — it schedules ``connection_lost`` — so the
        ``OSError`` guard is belt-and-braces, not the reaper.) Not draining is also what
        keeps a wedged client's backpressure out of the delivery barrier, which an emit
        holds across this call — see :func:`_frame_sender`.

        Nothing bounds a stalled client's write buffer today: #134 shipped stateless,
        so there is no per-connection subscription to filter it down, and the buffer is
        held in check only by the local-trust 0600 socket and small frames. A per-client
        outbound queue with a slow-client drop is the structural fix if one is ever
        needed; the async signature is kept for it.
        """
        wire = encode(frame)
        for writer in list(self._clients):
            if writer.is_closing():
                self._clients.discard(writer)
                continue
            try:
                writer.write(wire)
            except OSError:
                self._clients.discard(writer)

    async def run(self) -> None:
        """Bind the socket and serve until cancelled or :meth:`stop`.

        Reaps a stale socket file first (an unclean prior shutdown would otherwise fail
        the bind with ``EADDRINUSE``). Sets :attr:`started` once the socket is bound and
        chmod'd, so a caller can await readiness before connecting.
        """
        socket_path = Path(self.path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)
        # There is a race between bind and chmod where the socket is briefly 0o755-ish;
        # we accept it rather than fiddle the process-wide umask around an await (that
        # would leak to every other file created on this loop in the window). The parent
        # data/ dir is user-owned, which bounds the exposure.
        os.chmod(self.path, _SOCKET_MODE)
        self.started.set()
        try:
            # start_unix_server() is already accepting; nothing to drive. Park until
            # stop() or cancellation — NOT serve_forever(): on 3.13 its CancelledError
            # handler awaits wait_closed() before our cleanup can close clients, which
            # deadlocks shutdown against any still-connected client.
            await self._stopped.wait()
        finally:
            # Runs on cancellation too (fixture teardown / gather cancel skips stop()),
            # so a bare cancel performs the same full shutdown. Idempotent with stop().
            await self._shutdown()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one connection: greet, then loop answering frames until EOF."""
        if self._closing:
            # Accepted in the window between accept and this task's first step,
            # after shutdown snapshotted the handler set — self-close instead of
            # parking, or wait_closed() below would block on our transport forever.
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            return
        task = asyncio.current_task()
        assert task is not None  # streams always run this callback inside a task
        self._handlers.add(task)
        try:
            writer.write(encode(hello_frame()))
            await writer.drain()
            # Claim-replay and joining the broadcast set are ONE critical section under
            # the delivery barrier (#134). Registering only after the hook is what makes
            # the hook's replay frames precede live traffic on this connection; holding
            # the barrier across both is what makes the claim and a racing emit agree on
            # what is held. Without it an emit could broadcast (to a set this writer has
            # not joined) and still be mid-``record``, so the claim would find nothing —
            # the frame reaching neither this client nor a later one except as a
            # re-delivery. See :attr:`delivery_lock`.
            async with self.delivery_lock:
                if self._connect_hook is not None:
                    try:
                        await self._connect_hook(_frame_sender(writer))
                    except Exception:
                        logger.exception("client-plane connect hook raised")
                self._clients.add(writer)  # register for outbound broadcast (#131)
            # Flush the replay only now the barrier is released — draining under it
            # would let one client that stopped reading freeze the whole daemon (see
            # :func:`_frame_sender`). The replay bytes are already queued ahead of any
            # live frame a broadcast can write here, and this wait cannot reorder them,
            # so the replay-before-live contract survives the deferral.
            await writer.drain()
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    # readline overran its stream limit — the frame is unframeable, so
                    # report and drop this connection (the stream is out of sync).
                    writer.write(encode(error_frame("line_too_long", "frame too long")))
                    await writer.drain()
                    break
                if not line:
                    break  # clean EOF: the client hung up
                await self._dispatch(line, writer)
        except (ConnectionResetError, BrokenPipeError):
            pass  # the client vanished mid-exchange — nothing left to answer
        finally:
            self._handlers.discard(task)
            self._clients.discard(writer)
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _dispatch(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        """Answer one inbound line: pong a ping, then offer it to the handler (#131).

        ``ping`` stays a transport concern the server answers itself, so it works even
        before any handler is installed. Any other frame is offered to the injected
        handler (if any); a handler that returns ``True`` has already answered through
        ``sender``. An unhandled frame (no handler, or handler returned ``False``) gets
        the #130 ``unknown_type`` error — so with no handler the behaviour is unchanged.
        A handler that raises is contained: the connection loop and daemon survive.
        """
        try:
            frame = decode(line)
        except FrameError as exc:
            writer.write(encode(error_frame(exc.code, str(exc))))
            await writer.drain()
            return
        if frame.get("type") == TYPE_PING:
            writer.write(encode(pong_frame()))
            await writer.drain()
            return
        if self._handler is not None:
            try:
                if await self._handler(frame, _frame_sender(writer)):
                    # Flush what the handler wrote. Deferred to here on purpose: a
                    # ``switch`` writes its backfill inside the delivery barrier, and
                    # draining there would freeze the daemon (see
                    # :func:`_frame_sender`). The handler has released the barrier by
                    # the time it returns.
                    await writer.drain()
                    return
            except Exception:
                logger.exception("client-plane frame handler raised")
                writer.write(
                    encode(error_frame("internal_error", "handler failed"))
                )
                await writer.drain()
                return
        writer.write(
            encode(
                error_frame(
                    "unknown_type",
                    f"unknown frame type: {frame.get('type')!r}",
                )
            )
        )
        await writer.drain()

    async def _shutdown(self) -> None:
        """Stop accepting, force-close live connections, remove the socket file.

        Order is load-bearing. Set :attr:`_closing` first so a connection accepted in
        the registration window (accept done, handler not yet in the set) self-closes
        instead of parking. Then stop *accepting*, then cancel each handler task: a
        handler parked on ``reader.readline()`` unblocks on cancel, its ``finally``
        closes the writer, and the transport detaches — only *then* does
        ``wait_closed()`` return (on 3.13 it blocks until every handler finishes, so
        cancelling first is what breaks the deadlock against a still-connected client).
        Idempotent: a second call sees ``_server is None`` and a drained snapshot.
        """
        self._closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()  # stop accepting; established connections stay open
        handlers = list(self._handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        Path(self.path).unlink(missing_ok=True)

    async def stop(self) -> None:
        """Shut the listener down; idempotent, safe to call twice.

        A thin wrapper over :meth:`_shutdown`. :attr:`_stopped` is set *last* so a
        ``run()`` parked on it wakes only after cleanup has finished — its own
        ``finally``-``_shutdown`` then finds nothing left to do (a no-op).
        """
        await self._shutdown()
        self._stopped.set()
