"""The always-on client-plane socket listener (#130).

:class:`SocketServer` binds a unix-domain socket and serves LF-delimited JSON frames
(:mod:`chief.client_plane.protocol`) to any number of concurrent clients. It is a
standalone lifecycle object — ``run()``/``stop()`` mirror the adapter names so
``app.serve`` reads uniformly — but it owns no engine or task manager: the socket is
unconditional infrastructure that stays up even on a zero-platform boot.
"""

import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path

from .protocol import FrameError, decode, encode, error_frame, hello_frame, pong_frame

logger = logging.getLogger(__name__)

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
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

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
            async with self._server:
                await self._server.serve_forever()
        finally:
            # serve_forever() is cancelled on shutdown; unlink here too so a cancel that
            # skips stop() (fixture teardown) still cleans up. Idempotent with stop().
            self._server = None
            Path(self.path).unlink(missing_ok=True)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one connection: greet, then loop answering frames until EOF."""
        self._writers.add(writer)
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
            self._writers.discard(writer)
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _dispatch(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        """Answer one inbound line: pong a ping, error anything else."""
        try:
            frame = decode(line)
        except FrameError as exc:
            writer.write(encode(error_frame(exc.code, str(exc))))
            await writer.drain()
            return
        if frame.get("type") == "ping":
            writer.write(encode(pong_frame()))
        else:
            writer.write(
                encode(
                    error_frame(
                        "unknown_type",
                        f"unknown frame type: {frame.get('type')!r}",
                    )
                )
            )
        await writer.drain()

    async def stop(self) -> None:
        """Stop accepting, close every live connection, and remove the socket file.

        Idempotent — safe to call twice (shutdown may race the run() finally). Note
        ``Server.close()`` only stops *accepting*; the explicit writer sweep is what
        closes already-established connections, and it is version-independent (unlike
        ``Server.close_clients()``, which is new and behaves differently across minors).

        The sweep runs *before* ``wait_closed()``: on 3.13 ``wait_closed()`` blocks
        until every connection handler task finishes, and a handler parked on
        ``reader.readline()`` unblocks only once we close its writer — so waiting first
        would deadlock shutdown against a still-connected client.
        """
        server, self._server = self._server, None
        if server is not None:
            server.close()  # stop accepting; established connections stay open
        for writer in list(self._writers):
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        self._writers.clear()
        if server is not None:
            await server.wait_closed()
        Path(self.path).unlink(missing_ok=True)
