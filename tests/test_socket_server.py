"""End-to-end tests for the client-plane socket listener (#130).

Every test drives a REAL, bound unix-domain socket: a real client connects with
``asyncio.open_unix_connection`` and exchanges real frames with a running
:class:`SocketServer`. The central mechanism is never mocked.
"""

import asyncio
import json
import os
import stat
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest

from chief.client_plane import SocketServer


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, object]:
    line = await asyncio.wait_for(reader.readline(), 5)
    frame: dict[str, object] = json.loads(line)
    return frame


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[SocketServer]:
    srv = SocketServer(str(tmp_path / "s.sock"))
    task = asyncio.create_task(srv.run())
    await asyncio.wait_for(srv.started.wait(), 5)
    try:
        yield srv
    finally:
        await srv.stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_client_receives_versioned_hello(server: SocketServer) -> None:
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert await _read_frame(reader) == {"type": "hello", "protocol": 1}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_ping_returns_pong(server: SocketServer) -> None:
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(b'{"type":"ping"}\n')
        await writer.drain()
        assert await _read_frame(reader) == {"type": "pong"}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_malformed_json_gets_error_then_loop_survives(
    server: SocketServer,
) -> None:
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(b"{oops\n")
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error"
        assert error["code"] == "invalid_json"
        # The handler loop must survive bad input — ping still answers on the SAME
        # connection (no daemon crash, no dropped connection).
        writer.write(b'{"type":"ping"}\n')
        await writer.drain()
        assert await _read_frame(reader) == {"type": "pong"}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_unknown_type_and_missing_type_get_unknown_type(
    server: SocketServer,
) -> None:
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(b'{"type":"nope"}\n')
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "unknown_type"
        writer.write(b"{}\n")
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "unknown_type"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_non_object_frame_gets_invalid_frame(server: SocketServer) -> None:
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(b"[1,2]\n")
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "invalid_frame"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_two_clients_function_simultaneously(server: SocketServer) -> None:
    r1, w1 = await asyncio.open_unix_connection(server.path)
    r2, w2 = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(r1))["type"] == "hello"
        assert (await _read_frame(r2))["type"] == "hello"
        # Interleave the two connections to prove they are independent.
        for reader, writer in ((r1, w1), (r2, w2), (r1, w1)):
            writer.write(b'{"type":"ping"}\n')
            await writer.drain()
            assert await _read_frame(reader) == {"type": "pong"}
    finally:
        for writer in (w1, w2):
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


async def test_socket_file_is_owner_only(server: SocketServer) -> None:
    assert stat.S_IMODE(os.stat(server.path).st_mode) == 0o600


async def test_stop_closes_connections_and_removes_file(tmp_path: Path) -> None:
    # A locally built server (not the fixture) so we can assert stop() is idempotent
    # by calling it twice without the fixture teardown racing us.
    srv = SocketServer(str(tmp_path / "local.sock"))
    task = asyncio.create_task(srv.run())
    await asyncio.wait_for(srv.started.wait(), 5)
    reader, writer = await asyncio.open_unix_connection(srv.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"

        await srv.stop()

        # The established connection is closed server-side → the client reads EOF.
        assert await asyncio.wait_for(reader.readline(), 5) == b""
        assert not Path(srv.path).exists()
        # Idempotent: a second stop is a no-op, not an error.
        await srv.stop()
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_stale_socket_file_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "stale.sock"
    path.touch()  # a leftover file from an unclean prior shutdown
    srv = SocketServer(str(path))
    task = asyncio.create_task(srv.run())
    try:
        await asyncio.wait_for(srv.started.wait(), 5)
        # Binding succeeded despite the pre-existing file — a client can connect.
        reader, writer = await asyncio.open_unix_connection(srv.path)
        assert (await _read_frame(reader))["type"] == "hello"
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
    finally:
        await srv.stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
