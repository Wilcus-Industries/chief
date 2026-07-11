"""The always-on client-plane socket listener (#130, #131).

:class:`SocketServer` binds a unix-domain socket and serves LF-delimited JSON frames
(:mod:`chief.client_plane.protocol`) to any number of concurrent clients. It is a
standalone lifecycle object — ``run()``/``stop()`` mirror the adapter names so
``app.serve`` reads uniformly — and it stays **transport-only**: it owns no engine or
task manager, so the socket is unconditional infrastructure that stays up even on a
zero-platform boot. The CLI adapter (#131) injects an application via
:meth:`set_handler` (inbound dispatch) and pushes engine output out via
:meth:`broadcast`.
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
#: turn output fans out to every client via :meth:`SocketServer.broadcast`.
FrameSender = Callable[[Mapping[str, object]], Awaitable[None]]

#: An inbound-frame application: ``(frame, sender) -> handled``. Returns ``True`` when
#: it owns the frame (it has already answered through ``sender``), ``False`` to let the
#: server fall back to its ``unknown_type`` error. Transport frames (``ping``) never
#: reach it — the server answers those itself.
FrameHandler = Callable[
    [Mapping[str, object], FrameSender], Awaitable[bool]
]

#: Socket file mode: owner read/write only. The socket is a local-control surface, so no
#: group/other access — mirrors the db_path fence class (config.SELF_CONFIG_DENYLIST).
_SOCKET_MODE = 0o600


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
        #: Live client writers, for outbound push (#131). Registered/discarded in
        #: :meth:`_handle` alongside the handler task; :meth:`broadcast` snapshots it.
        self._clients: set[asyncio.StreamWriter] = set()
        #: The injected inbound application (#131). ``None`` ⇒ transport-only (#130):
        #: every non-ping frame falls through to the ``unknown_type`` error.
        self._handler: FrameHandler | None = None

    def set_handler(self, handler: FrameHandler) -> None:
        """Install the inbound-frame application (the CLI adapter, #131).

        Before this is called the server is byte-identical to #130: pings pong,
        everything else errors ``unknown_type``. After, each decoded non-ping frame is
        offered to ``handler`` first.
        """
        self._handler = handler

    async def broadcast(self, frame: Mapping[str, object]) -> None:
        """Push ``frame`` to every live client — engine output fans out here (#131).

        Deliberately does **not** ``drain()``: it is awaited from inside the engine's
        turn loop (``_run_turn`` → ``CliTaskIO.send``), so blocking on a wedged client's
        backpressure would freeze the turn for every thread. A write to a closing/dead
        writer raises, and that client is dropped (its handler's ``readline`` will hit
        EOF and clean up the rest). Unbounded write buffers are bounded in practice by
        the local-trust 0600 socket and small frames; #134 subscriptions are the
        structural fix, and the async signature is kept for it.
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
        self._clients.add(writer)  # register for outbound broadcast (#131)
        try:
            writer.write(encode(hello_frame()))
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
            async def sender(reply: Mapping[str, object]) -> None:
                writer.write(encode(reply))
                await writer.drain()

            try:
                if await self._handler(frame, sender):
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
