"""End-to-end tests for the client-plane socket listener (#130).

Every test drives a REAL, bound unix-domain socket: a real client connects with
``asyncio.open_unix_connection`` and exchanges real frames with a running
:class:`SocketServer`. The central mechanism is never mocked.
"""

import asyncio
import json
import os
import stat
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from pathlib import Path

import pytest

from chief.client_plane import FrameSender, SocketServer


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


async def test_oversized_line_gets_line_too_long_and_closes(
    server: SocketServer,
) -> None:
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(b"x" * (2**16 + 1) + b"\n")
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "line_too_long"
        assert await asyncio.wait_for(reader.readline(), 5) == b""
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


async def test_cancel_with_connected_client_completes(tmp_path: Path) -> None:
    # Regression (#130 review): the production shutdown path is CANCELLATION of
    # run() while a client is still connected. On 3.13 serve_forever()'s cancel
    # handler awaited wait_closed() with the client's transport still attached,
    # hanging shutdown forever. Nothing here disconnects before the cancel.
    srv = SocketServer(str(tmp_path / "hang.sock"))
    task = asyncio.create_task(srv.run())
    await asyncio.wait_for(srv.started.wait(), 5)
    reader, writer = await asyncio.open_unix_connection(srv.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        task.cancel()  # client still connected and parked — the deadlock shape
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not Path(srv.path).exists()
        # The server force-closed the connection: the client reads EOF.
        assert await asyncio.wait_for(reader.readline(), 5) == b""
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_broadcast_reaches_every_live_client(server: SocketServer) -> None:
    # #131 push seam: engine output fans out to all clients tagged by thread_key.
    r1, w1 = await asyncio.open_unix_connection(server.path)
    r2, w2 = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(r1))["type"] == "hello"
        assert (await _read_frame(r2))["type"] == "hello"
        # A handler must be registered for the writers to exist; broadcast is orthogonal
        # to inbound handling, so we can push straight away once both are connected.
        await asyncio.sleep(0.05)  # let both handler tasks register their writers
        await server.broadcast({"type": "reply", "text": "hi"})
        assert await _read_frame(r1) == {"type": "reply", "text": "hi"}
        assert await _read_frame(r2) == {"type": "reply", "text": "hi"}
    finally:
        for writer in (w1, w2):
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


async def test_broadcast_skips_a_dropped_client_and_reaches_survivor(
    server: SocketServer,
) -> None:
    # A dead client must not break delivery to a live one (no drain, dropped on error).
    r1, w1 = await asyncio.open_unix_connection(server.path)
    r2, w2 = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(r1))["type"] == "hello"
        assert (await _read_frame(r2))["type"] == "hello"
        await asyncio.sleep(0.05)
        # Client 1 hangs up; the server hasn't noticed yet.
        w1.close()
        with suppress(OSError):
            await w1.wait_closed()
        await asyncio.sleep(0.05)
        await server.broadcast({"type": "reply", "text": "survivor"})
        assert await _read_frame(r2) == {"type": "reply", "text": "survivor"}
    finally:
        for writer in (w1, w2):
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


async def test_broadcast_with_zero_clients_is_a_noop(server: SocketServer) -> None:
    # No connections registered → broadcast returns cleanly, no error.
    await server.broadcast({"type": "reply", "text": "nobody"})


async def test_handler_owns_a_frame_and_answers_through_sender(
    server: SocketServer,
) -> None:
    # #131 inbound seam: an installed handler answers a frame it owns via the sender.
    async def handler(frame: Mapping[str, object], sender: FrameSender) -> bool:
        if frame.get("type") == "user":
            await sender({"type": "reply", "text": f"got {frame.get('text')}"})
            return True
        return False

    server.set_handler(handler)
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(b'{"type":"user","text":"hi"}\n')
        await writer.drain()
        assert await _read_frame(reader) == {"type": "reply", "text": "got hi"}
        # Transport frames still answer even with a handler installed.
        writer.write(b'{"type":"ping"}\n')
        await writer.drain()
        assert await _read_frame(reader) == {"type": "pong"}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_handler_declining_a_frame_falls_through_to_unknown_type(
    server: SocketServer,
) -> None:
    async def handler(frame: Mapping[str, object], sender: FrameSender) -> bool:
        return False  # owns nothing

    server.set_handler(handler)
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(b'{"type":"user","text":"hi"}\n')
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "unknown_type"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_clearing_the_handler_restores_unknown_type_fallthrough(
    server: SocketServer,
) -> None:
    # set_handler(None) detaches the app (shutdown path) — the server is #130 again.
    async def handler(frame: Mapping[str, object], sender: FrameSender) -> bool:
        await sender({"type": "reply", "text": "owned"})
        return True

    server.set_handler(handler)
    server.set_handler(None)
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(b'{"type":"user","text":"hi"}\n')
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "unknown_type"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_raising_handler_gets_internal_error_and_loop_survives(
    server: SocketServer,
) -> None:
    async def handler(frame: Mapping[str, object], sender: FrameSender) -> bool:
        raise RuntimeError("boom")

    server.set_handler(handler)
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(b'{"type":"user","text":"hi"}\n')
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "internal_error"
        # The connection loop survived a raising handler — ping still pongs.
        writer.write(b'{"type":"ping"}\n')
        await writer.drain()
        assert await _read_frame(reader) == {"type": "pong"}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_connect_hook_frames_arrive_after_hello_before_live_traffic(
    server: SocketServer,
) -> None:
    # #132 replay seam: a connect hook runs once per accepted connection, after the
    # hello and before the connection joins the broadcast set. Frames it sends through
    # its sender precede any live traffic on that connection.
    async def hook(sender: FrameSender) -> None:
        await sender({"type": "reply", "text": "replayed"})

    server.set_connect_hook(hook)
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        assert await _read_frame(reader) == {"type": "reply", "text": "replayed"}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_client_not_registered_until_connect_hook_completes(
    server: SocketServer,
) -> None:
    # The joining client must not be in the broadcast set while its hook runs — a
    # broadcast during the hook is never delivered to it (it replays on the next
    # attach instead), so replay frames can never interleave with live traffic.
    hook_entered = asyncio.Event()
    release = asyncio.Event()

    async def hook(sender: FrameSender) -> None:
        hook_entered.set()
        await release.wait()

    server.set_connect_hook(hook)
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        await asyncio.wait_for(hook_entered.wait(), 5)
        assert server.has_clients is False
        await server.broadcast({"type": "reply", "text": "missed"})
        release.set()
        # The broadcast never reached the joining client: the next frame is the pong.
        writer.write(b'{"type":"ping"}\n')
        await writer.drain()
        assert await _read_frame(reader) == {"type": "pong"}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_connect_hook_exception_is_contained(server: SocketServer) -> None:
    # A raising connect hook must not kill the connection or the daemon.
    async def hook(sender: FrameSender) -> None:
        raise RuntimeError("boom")

    server.set_connect_hook(hook)
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
