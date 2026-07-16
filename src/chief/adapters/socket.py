"""CLI/socket adapter: the local control plane over a unix domain socket.

Protocol is newline-delimited JSON. Inbound: {"thread": str, "text": str}.
Outbound: {"type": "delta"|"final", "thread": str, "text": str}. A local
socket connection is by definition the owner.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from chief.adapters.base import Adapter, Message

logger = logging.getLogger(__name__)

HandleMessage = Callable[[Message], Coroutine[Any, Any, None]]


class SocketAdapter(Adapter):
    """Serves the unix socket and bridges frames to the dispatcher."""

    name = "cli"

    def __init__(self, socket_path: Path, handle: HandleMessage) -> None:
        self._socket_path = socket_path
        self._handle = handle
        self._server: asyncio.Server | None = None
        self._writers: dict[str, asyncio.StreamWriter] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._on_connect, path=str(self._socket_path)
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in self._tasks:
            task.cancel()
        self._socket_path.unlink(missing_ok=True)

    async def send(self, thread_key: str, text: str) -> None:
        await self._write(thread_key, {"type": "final", "text": text})

    async def send_delta(self, thread_key: str, text: str) -> None:
        await self._write(thread_key, {"type": "delta", "text": text})

    async def _write(self, thread_key: str, frame: dict[str, str]) -> None:
        writer = self._writers.get(thread_key)
        if writer is None:
            logger.warning(
                "no live connection for thread %s; dropping frame", thread_key
            )
            return
        writer.write(
            json.dumps({"thread": thread_key, **frame}).encode() + b"\n"
        )
        await writer.drain()

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while line := await reader.readline():
                self._on_frame(line, writer)
        finally:
            self._drop_writer(writer)
            writer.close()

    def _on_frame(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            frame = json.loads(line)
            thread = str(frame.get("thread") or "main")
            text = str(frame["text"])
        except (json.JSONDecodeError, KeyError):
            logger.warning("dropping malformed socket frame: %r", line)
            return
        thread_key = f"cli:{thread}"
        # Last connection to speak on a thread receives its replies.
        self._writers[thread_key] = writer
        message = Message(
            channel=self.name, sender="owner", thread_key=thread_key, text=text
        )
        task = asyncio.create_task(self._handle(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _drop_writer(self, writer: asyncio.StreamWriter) -> None:
        for key in [k for k, w in self._writers.items() if w is writer]:
            del self._writers[key]
