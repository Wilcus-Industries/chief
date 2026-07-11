"""The client's socket wire seam: connect, send, and iterate frames (#137).

Kept free of any Textual code so it can be exercised (and reused) without a UI, and so
:mod:`chief.cli.app` never touches ``asyncio.StreamReader``/``StreamWriter`` directly.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from typing import Final

from ..client_plane import FrameError, decode, encode

logger = logging.getLogger(__name__)

#: asyncio's default ``StreamReader.readline`` limit is 64 KiB, far under a ``file``
#: frame's base64 payload (``adapters/cli.py: CLI_LIMIT = 1_000_000`` bytes of text can
#: cross the file threshold at ~4x that, then base64-inflate by another third) — an
#: overrun would make the daemon close the connection with ``line_too_long``. 16 MiB
#: leaves generous headroom over that worst case.
READ_LIMIT: Final[int] = 16 * 1024 * 1024


class SocketConnection:
    """A thin asyncio unix-socket client speaking the client-plane frame protocol."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        """Dial the socket. Lets ``FileNotFoundError``/``ConnectionRefusedError``
        propagate — the caller turns those into a plain "daemon not running" message.
        """
        self._reader, self._writer = await asyncio.open_unix_connection(
            self.path, limit=READ_LIMIT
        )

    async def send(self, frame: Mapping[str, object]) -> None:
        """Write one frame to the daemon."""
        assert self._writer is not None, "send() called before connect()"
        self._writer.write(encode(frame))
        await self._writer.drain()

    async def frames(self) -> AsyncIterator[dict[str, object]]:
        """Yield decoded frames until the daemon closes the connection (clean EOF).

        A line that fails to decode is logged and skipped rather than crashing the
        pump — one malformed line from the daemon should not take down the client.
        """
        assert self._reader is not None, "frames() called before connect()"
        while True:
            line = await self._reader.readline()
            if not line:
                return
            try:
                yield decode(line)
            except FrameError:
                logger.exception("client-plane client could not decode frame")

    async def close(self) -> None:
        """Close the connection. Idempotent — safe to call more than once."""
        if self._writer is None:
            return
        writer, self._writer = self._writer, None
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
