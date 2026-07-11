"""End-to-end tests for the terminal client of the client plane (#137).

The central mechanism — a real Textual app driving a real unix-domain socket — is
exercised for real: :class:`ChiefCliApp` connects a real :class:`SocketConnection` to
a :class:`ScriptedPeer`, a fake daemon built on a real ``asyncio.start_unix_server``,
not a mocked connection. ``ScriptedPeer`` stands in for the real daemon
(``CliAdapter`` + ``SocketServer``, already covered end-to-end by
``tests/test_cli_platform.py``) so these tests isolate the client's rendering and
input handling.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from pathlib import Path

import pytest

from chief.adapters.commands import OWNER_COMMANDS
from chief.cli.app import AWAY_FOOTER, AWAY_HEADER, ChiefCliApp, Line
from chief.cli.connection import SocketConnection
from chief.client_plane import (
    PROTOCOL_VERSION,
    answer_frame,
    card_frame,
    command_frame,
    decode,
    encode,
    error_frame,
    hello_frame,
    milestone_frame,
    reply_frame,
    user_frame,
)


class ScriptedPeer:
    """A fake daemon: records inbound frames, pushes outbound ones on demand."""

    def __init__(self) -> None:
        self.received: list[dict[str, object]] = []
        self.connected = asyncio.Event()
        self.disconnected = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def start(self, path: str) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=path)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writer = writer
        writer.write(encode(hello_frame()))
        await writer.drain()
        self.connected.set()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                self.received.append(decode(line))
        finally:
            # Detach fully (not just half-close) so Server.wait_closed() — which
            # waits for the server closed AND every connection dropped — returns.
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.disconnected.set()

    async def push(self, frame: Mapping[str, object]) -> None:
        assert self._writer is not None, "push() before a client connected"
        self._writer.write(encode(frame))
        await self._writer.drain()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, object]:
    line = await asyncio.wait_for(reader.readline(), 5)
    frame: dict[str, object] = json.loads(line)
    return frame


async def _settle(
    app: ChiefCliApp, predicate: Callable[[], bool], timeout: float = 2.0
) -> None:
    """Poll ``predicate`` until true — the pump worker needs real loop turns for a
    socket read; ``pilot.pause()`` alone does not wait for that.
    """

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


@pytest.fixture
def socket_path(tmp_path: Path) -> str:
    return str(tmp_path / "chief.sock")


@pytest.fixture
async def peer(socket_path: str) -> AsyncIterator[ScriptedPeer]:
    p = ScriptedPeer()
    await p.start(socket_path)
    try:
        yield p
    finally:
        await p.stop()


@pytest.fixture
async def conn(socket_path: str, peer: ScriptedPeer) -> AsyncIterator[SocketConnection]:
    c = SocketConnection(socket_path)
    await c.connect()
    await asyncio.wait_for(peer.connected.wait(), 5)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
def app(conn: SocketConnection) -> ChiefCliApp:
    return ChiefCliApp(conn)


async def test_typing_a_message_emits_a_user_frame(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"hi", "enter")
        await _settle(app, lambda: bool(peer.received))
        assert peer.received[-1] == user_frame("cli:main", "hi")
        assert Line("bold", "> hi") in app.transcript


async def test_milestone_then_reply_render_in_order_with_styles(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test():
        await peer.push(milestone_frame("cli:main", "reading file"))
        await peer.push(reply_frame("cli:main", "done"))
        await _settle(app, lambda: any(line.text == "done" for line in app.transcript))
        texts = [line.text for line in app.transcript]
        assert texts.index("· reading file") < texts.index("done")
        milestone_line = next(
            line for line in app.transcript if line.text == "· reading file"
        )
        reply_line = next(line for line in app.transcript if line.text == "done")
        assert milestone_line.style == "dim"
        assert reply_line.style == ""


async def test_card_approve_sends_approve_once(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await peer.push(card_frame("cli:main", 7, "run rm -rf?"))
        await _settle(app, lambda: bool(app._pending))
        await pilot.press("y")
        await _settle(app, lambda: bool(peer.received))
        assert peer.received[-1] == answer_frame(7, "approve_once")


async def test_card_deny_sends_deny_once(app: ChiefCliApp, peer: ScriptedPeer) -> None:
    async with app.run_test() as pilot:
        await peer.push(card_frame("cli:main", 7, "run rm -rf?"))
        await _settle(app, lambda: bool(app._pending))
        await pilot.press("n")
        await _settle(app, lambda: bool(peer.received))
        assert peer.received[-1] == answer_frame(7, "deny_once")


async def test_replay_section_banded_by_header_and_footer(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test():
        await peer.push({**reply_frame("cli:main", "missed one"), "replay": True})
        await peer.push({**reply_frame("cli:main", "missed two"), "replay": True})
        await peer.push(reply_frame("cli:main", "live"))
        await _settle(app, lambda: any(line.text == "live" for line in app.transcript))
        texts = [line.text for line in app.transcript]
        assert (
            texts.index(AWAY_HEADER)
            < texts.index("missed one")
            < texts.index("missed two")
            < texts.index(AWAY_FOOTER)
            < texts.index("live")
        )


async def test_new_switches_thread_and_clears_transcript(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"one", "enter")
        await _settle(app, lambda: len(peer.received) == 1)
        await pilot.press(*"/new", "enter")

        def _new_thread_announced() -> bool:
            return any(line.text.startswith("new thread:") for line in app.transcript)

        await _settle(app, _new_thread_announced)
        assert app.transcript[0].text.startswith("new thread:")
        await pilot.press(*"two", "enter")
        await _settle(app, lambda: len(peer.received) == 2)
        second = peer.received[1]
        assert second == user_frame(str(second["thread_key"]), "two")
        assert second["thread_key"] != "cli:main"
        assert str(second["thread_key"]).startswith("cli:")


async def test_quit_closes_socket_and_daemon_keeps_serving(
    app: ChiefCliApp, peer: ScriptedPeer, socket_path: str
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/quit", "enter")
        await _settle(app, lambda: peer.disconnected.is_set())

    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        hello = await _read_frame(reader)
        assert hello == {"type": "hello", "protocol": PROTOCOL_VERSION}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_help_lists_client_and_owner_commands(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/help", "enter")
        await _settle(app, lambda: len(app.transcript) >= 2)
        blob = "\n".join(line.text for line in app.transcript)
        for name in OWNER_COMMANDS.names():
            assert f"/{name}" in blob
        assert "/new" in blob
        assert "/quit" in blob


async def test_other_platform_and_thread_frames_filtered_but_cards_shown(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test():
        await peer.push(reply_frame("cli:main", "telegram reply", platform="telegram"))
        await peer.push(reply_frame("cli:other", "other thread reply"))
        await peer.push(
            card_frame("cli:main", 9, "cross-platform card", platform="telegram")
        )
        await _settle(app, lambda: bool(app._pending))
        blob = "\n".join(line.text for line in app.transcript)
        assert "telegram reply" not in blob
        assert "other thread reply" not in blob
        assert "cross-platform card" in blob


async def test_unknown_command_forwarded_then_error_rendered(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/bogus", "enter")
        await _settle(app, lambda: bool(peer.received))
        assert peer.received[-1] == command_frame("cli:main", "bogus", "")
        await peer.push(error_frame("unknown_command", "unknown command: 'bogus'"))
        await _settle(app, lambda: any(line.style == "red" for line in app.transcript))
        assert any(
            line.style == "red" and line.text.startswith("!") for line in app.transcript
        )
