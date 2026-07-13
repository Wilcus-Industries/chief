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
from typing import Any

import pytest
from textual.widgets import Footer, Static

from chief.adapters.commands import OWNER_COMMANDS
from chief.cli.app import (
    AWAY_FOOTER,
    AWAY_HEADER,
    OWNER_STYLE,
    ChiefCliApp,
    Line,
    PromptInput,
)
from chief.cli.connection import SocketConnection
from chief.client_plane import (
    PROTOCOL_VERSION,
    answer_frame,
    backfill_frame,
    card_frame,
    command_frame,
    decode,
    encode,
    error_frame,
    hello_frame,
    inject_frame,
    list_threads_frame,
    milestone_frame,
    reply_frame,
    skills_frame,
    skills_list_frame,
    status_frame,
    status_snapshot_frame,
    switch_frame,
    threads_frame,
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
        assert Line(OWNER_STYLE, "> hi") in app.transcript


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
        # on_mount sends one status_frame() request (#138) before any keystroke.
        await _settle(app, lambda: len(peer.received) == 1)
        await pilot.press(*"one", "enter")
        await _settle(app, lambda: len(peer.received) == 2)
        await pilot.press(*"/new", "enter")

        def _new_thread_announced() -> bool:
            return any(line.text.startswith("new thread:") for line in app.transcript)

        await _settle(app, _new_thread_announced)
        assert app.transcript[0].text.startswith("new thread:")
        await pilot.press(*"two", "enter")
        await _settle(app, lambda: len(peer.received) == 3)
        second = peer.received[2]
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
        # #138: a background thread's traffic is never silently invisible — each of
        # the two off-pane replies leaves a one-line notice instead of vanishing.
        assert "telegram" in blob and "cli:main" in blob
        assert "cli:other" in blob


async def test_malformed_frame_does_not_kill_pump(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test():
        await peer.push({"type": "reply", "thread_key": "cli:main", "text": "no plat"})
        await peer.push({"type": "card", "platform": "cli", "approval_id": "bad"})
        await peer.push(reply_frame("cli:main", "still alive"))
        await _settle(
            app, lambda: any(line.text == "still alive" for line in app.transcript)
        )
        assert any(
            line.style == "red" and "malformed" in line.text
            for line in app.transcript
        )
        assert not app._pending


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


# ---- #138: /tasks, /switch, /status, /skills ---------------------------------------


async def test_tasks_lists_threads_across_platforms(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/tasks", "enter")
        await _settle(app, lambda: peer.received[-1].get("type") == "list_threads")
        assert peer.received[-1] == list_threads_frame()

        await peer.push(
            threads_frame(
                [
                    {
                        "platform": "cli", "thread_key": "cli:main",
                        "title": None, "status": "open",
                    },
                    {
                        "platform": "telegram", "thread_key": "-100:5",
                        "title": "chat", "status": "open",
                    },
                ]
            )
        )
        await _settle(
            app, lambda: any("cli:main" in line.text for line in app.transcript)
        )
        blob = "\n".join(line.text for line in app.transcript)
        assert "cli:main" in blob
        assert "-100:5" in blob


async def test_switch_flips_pane_and_renders_backfill(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/tasks", "enter")
        await _settle(app, lambda: peer.received[-1].get("type") == "list_threads")
        await peer.push(
            threads_frame(
                [
                    {
                        "platform": "telegram", "thread_key": "-100:5",
                        "title": "chat", "status": "open",
                    },
                ]
            )
        )
        await _settle(app, lambda: bool(app._last_threads))

        await pilot.press(*"/switch 1", "enter")
        await _settle(app, lambda: peer.received[-1].get("type") == "switch")
        assert peer.received[-1] == switch_frame("telegram", "-100:5")

        await peer.push(
            backfill_frame(
                "telegram",
                "-100:5",
                [
                    {
                        "role": "owner", "kind": "user", "text": "hi",
                        "filename": None, "created_at": "2026-01-01T00:00:00",
                    },
                    {
                        "role": "chief", "kind": "reply", "text": "hello",
                        "filename": None, "created_at": "2026-01-01T00:00:01",
                    },
                ],
            )
        )
        await _settle(app, lambda: any("hello" in line.text for line in app.transcript))
        assert app._active_platform == "telegram"
        assert app._thread_key == "-100:5"
        blob = "\n".join(line.text for line in app.transcript)
        assert "> hi" in blob
        assert "hello" in blob

        await peer.push(reply_frame("-100:5", "live reply", platform="telegram"))
        await _settle(
            app, lambda: any("live reply" in line.text for line in app.transcript)
        )
        live_line = next(
            line for line in app.transcript if "live reply" in line.text
        )
        assert live_line.text == "(telegram) live reply"


async def test_foreign_pane_blocks_user_message_input(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    """After /switch onto a foreign-platform thread, typed text must not be sent as a
    ``user`` frame — that would dispatch through the ``platform=cli`` engine under the
    foreign ``thread_key``, spawning a spurious cli task (#138 finding).
    """
    async with app.run_test() as pilot:
        await pilot.press(*"/tasks", "enter")
        await _settle(app, lambda: peer.received[-1].get("type") == "list_threads")
        await peer.push(
            threads_frame(
                [
                    {
                        "platform": "telegram", "thread_key": "-100:5",
                        "title": "chat", "status": "open",
                    },
                ]
            )
        )
        await _settle(app, lambda: bool(app._last_threads))

        await pilot.press(*"/switch 1", "enter")
        await _settle(app, lambda: peer.received[-1].get("type") == "switch")
        await peer.push(backfill_frame("telegram", "-100:5", []))
        await _settle(app, lambda: app._active_platform == "telegram")

        n = len(peer.received)
        await pilot.press(*"hello", "enter")
        await _settle(
            app, lambda: any(line.style == "red" for line in app.transcript)
        )
        assert any(
            line.style == "red" and "read-only pane" in line.text
            for line in app.transcript
        )
        assert len(peer.received) == n
        assert not any(f.get("type") == "user" for f in peer.received)


async def _switch_onto_telegram(
    app: ChiefCliApp, peer: ScriptedPeer, pilot: Any
) -> None:
    """Drive the client onto a foreign (telegram) pane via /tasks + /switch."""
    await pilot.press(*"/tasks", "enter")
    await _settle(app, lambda: peer.received[-1].get("type") == "list_threads")
    await peer.push(
        threads_frame(
            [
                {
                    "platform": "telegram", "thread_key": "-100:5",
                    "title": "chat", "status": "open",
                },
            ]
        )
    )
    await _settle(app, lambda: bool(app._last_threads))
    await pilot.press(*"/switch 1", "enter")
    await _settle(app, lambda: peer.received[-1].get("type") == "switch")
    await peer.push(backfill_frame("telegram", "-100:5", []))
    await _settle(app, lambda: app._active_platform == "telegram")


async def test_drive_sends_an_inject_frame_for_the_foreign_pane(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    """``/drive`` is the owner's only way to reach #135's cross-stack drive: it runs the
    text as a real owner turn on the *foreign* thread the pane is switched onto, so the
    answer lands in that platform's real chat. Without it the inject frame ships with no
    client that can send one, and the read-only pane (#138) has no escape hatch.
    """
    async with app.run_test() as pilot:
        await _switch_onto_telegram(app, peer, pilot)

        await pilot.press(*"/drive buy milk", "enter")
        await _settle(app, lambda: peer.received[-1].get("type") == "inject")

        assert peer.received[-1] == inject_frame("telegram", "-100:5", "buy milk")
        # The pane stays foreign and read-only — a drive is one turn, not an attach.
        assert app._active_platform == "telegram"
        assert app._thread_key == "-100:5"
        # The owner sees what they sent, marked as having gone out via the CLI.
        assert any("buy milk" in line.text for line in app.transcript)


async def test_drive_on_a_cli_pane_is_rejected(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    """On the client's own pane a drive is meaningless — that is what typing is. Sending
    an inject for a ``cli`` thread would ask the daemon to drive its own stack.
    """
    async with app.run_test() as pilot:
        n = len(peer.received)
        await pilot.press(*"/drive hello", "enter")
        await _settle(app, lambda: any(line.style == "red" for line in app.transcript))

        assert not any(f.get("type") == "inject" for f in peer.received)
        assert len(peer.received) == n


async def test_drive_without_text_is_rejected(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await _switch_onto_telegram(app, peer, pilot)

        n = len(peer.received)
        await pilot.press(*"/drive", "enter")
        await _settle(app, lambda: any(line.style == "red" for line in app.transcript))

        assert not any(f.get("type") == "inject" for f in peer.received)
        assert len(peer.received) == n


async def test_foreign_pane_blocks_forwarded_owner_command(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    """A forwarded slash command (not one of the client-only ones) must also be
    refused while a foreign-platform pane is active (#138 finding).
    """
    async with app.run_test() as pilot:
        await pilot.press(*"/tasks", "enter")
        await _settle(app, lambda: peer.received[-1].get("type") == "list_threads")
        await peer.push(
            threads_frame(
                [
                    {
                        "platform": "telegram", "thread_key": "-100:5",
                        "title": "chat", "status": "open",
                    },
                ]
            )
        )
        await _settle(app, lambda: bool(app._last_threads))

        await pilot.press(*"/switch 1", "enter")
        await _settle(app, lambda: peer.received[-1].get("type") == "switch")
        await peer.push(backfill_frame("telegram", "-100:5", []))
        await _settle(app, lambda: app._active_platform == "telegram")

        n = len(peer.received)
        await pilot.press(*"/bogus", "enter")
        await _settle(
            app, lambda: any(line.style == "red" for line in app.transcript)
        )
        assert any(
            line.style == "red" and "read-only pane" in line.text
            for line in app.transcript
        )
        assert len(peer.received) == n
        assert not any(f.get("type") == "command" for f in peer.received)


async def test_switch_out_of_range_is_rejected(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/switch 99", "enter")
        await _settle(
            app, lambda: any(line.style == "red" for line in app.transcript)
        )
        assert any(
            line.style == "red" and "usage" in line.text for line in app.transcript
        )
        assert not any(
            isinstance(f, dict) and f.get("type") == "switch" for f in peer.received
        )


async def test_background_thread_traffic_shows_one_line_notice(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test():
        await peer.push(reply_frame("cli:other", "bg reply"))
        await _settle(
            app, lambda: any("cli:other" in line.text for line in app.transcript)
        )
        blob = "\n".join(line.text for line in app.transcript)
        assert "cli:other" in blob
        assert "bg reply" not in blob


async def test_status_renders_snapshot_and_updates_statusbar(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        # on_mount already sent one status frame; wait for it so the count below
        # isolates the frame the /status keypress itself sends (#138 finding — a
        # status frame is content-identical every time, so ``received[-1] ==
        # status_frame()`` is satisfied by the mount-time frame alone).
        await _settle(app, lambda: bool(peer.received))
        n = len(peer.received)

        await pilot.press(*"/status", "enter")
        await _settle(app, lambda: len(peer.received) == n + 1)
        assert peer.received[-1] == status_frame()

        await peer.push(
            status_snapshot_frame(
                tasks=[
                    {
                        "platform": "cli", "thread_key": "cli:main", "title": None,
                        "status": "open", "model": "claude-sonnet-4-6",
                    },
                ],
                budget=[
                    {
                        "currency": "premium_requests", "spent": 10.0,
                        "cap": 200.0, "mode": "normal",
                    },
                ],
                schedules=[],
            )
        )
        await _settle(
            app,
            lambda: any(
                "claude-sonnet-4-6" in line.text for line in app.transcript
            ),
        )
        blob = "\n".join(line.text for line in app.transcript)
        assert "claude-sonnet-4-6" in blob
        statusbar_text = str(app.query_one("#statusbar", Static).render())
        assert "claude-sonnet-4-6" in statusbar_text
        assert "10/200" in statusbar_text


async def test_skills_lists_composed_set(app: ChiefCliApp, peer: ScriptedPeer) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/skills", "enter")
        await _settle(app, lambda: peer.received[-1].get("type") == "skills")
        assert peer.received[-1] == skills_frame()

        await peer.push(skills_list_frame(["setup-morning-brief", "docx"]))
        await _settle(
            app, lambda: any("docx" in line.text for line in app.transcript)
        )
        blob = "\n".join(line.text for line in app.transcript)
        assert "setup-morning-brief" in blob
        assert "docx" in blob


# ---- slash autocomplete + no-arg pickers --------------------------------------------
#
# One Dropdown (an OptionList above the prompt) serves two jobs: live command-name
# completion while typing, and a picker for commands submitted with no argument
# (``/switch``, ``/cancel``). ``app.dropdown_options`` is the test seam — the labels
# currently offered, empty when hidden — mirroring the ``transcript`` seam philosophy.


async def test_typing_slash_prefix_offers_matching_commands(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/s")
        assert "/switch" in app.dropdown_options
        assert "/status" in app.dropdown_options
        assert "/skills" in app.dropdown_options
        assert "/sonnet" in app.dropdown_options
        assert "/help" not in app.dropdown_options


async def test_plain_text_never_opens_the_dropdown(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"status")
        assert app.dropdown_options == []


async def test_argument_typing_closes_the_dropdown(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/switch 1")
        assert app.dropdown_options == []


async def test_tab_completes_to_the_highlighted_command(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/sw", "tab")
        assert app.query_one("#prompt", PromptInput).value == "/switch"


async def test_enter_accepts_the_highlighted_command_and_submits(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/sk", "enter")
        await _settle(app, lambda: bool(peer.received) and
                      peer.received[-1].get("type") == "skills")
        assert peer.received[-1] == skills_frame()
        assert app.dropdown_options == []
        assert app.query_one("#prompt", PromptInput).value == ""


async def test_escape_hides_the_dropdown(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/s")
        assert app.dropdown_options
        await pilot.press("escape")
        assert app.dropdown_options == []


async def test_bare_switch_opens_a_thread_picker(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    """``/switch`` with no argument fetches fresh threads and offers them as a menu —
    no more mandatory ``/tasks`` first."""
    async with app.run_test() as pilot:
        await pilot.press(*"/switch", "enter")
        await _settle(app, lambda: bool(peer.received) and
                      peer.received[-1].get("type") == "list_threads")

        await peer.push(
            threads_frame(
                [
                    {
                        "platform": "cli", "thread_key": "cli:main",
                        "title": None, "status": "open",
                    },
                    {
                        "platform": "telegram", "thread_key": "-100:5",
                        "title": "chat", "status": "open",
                    },
                ]
            )
        )
        await _settle(app, lambda: len(app.dropdown_options) == 2)
        assert "-100:5" in app.dropdown_options[1]

        await pilot.press("down", "enter")
        await _settle(app, lambda: peer.received[-1].get("type") == "switch")
        assert peer.received[-1] == switch_frame("telegram", "-100:5")
        assert app.dropdown_options == []


async def test_bare_cancel_offers_only_cli_threads(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    """``/cancel`` pickers over cli threads only — a ``command`` frame dispatches on
    the cli engine, so cancelling a foreign platform's thread there is a no-op."""
    async with app.run_test() as pilot:
        await pilot.press(*"/cancel", "enter")
        await _settle(app, lambda: bool(peer.received) and
                      peer.received[-1].get("type") == "list_threads")

        await peer.push(
            threads_frame(
                [
                    {
                        "platform": "telegram", "thread_key": "-100:5",
                        "title": "chat", "status": "open",
                    },
                    {
                        "platform": "cli", "thread_key": "cli:main",
                        "title": None, "status": "open",
                    },
                ]
            )
        )
        await _settle(app, lambda: len(app.dropdown_options) == 1)
        assert "cli:main" in app.dropdown_options[0]

        await pilot.press("enter")
        await _settle(app, lambda: peer.received[-1].get("type") == "command")
        assert peer.received[-1] == command_frame("cli:main", "cancel", "")


async def test_picker_escape_sends_nothing(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/switch", "enter")
        await _settle(app, lambda: bool(peer.received) and
                      peer.received[-1].get("type") == "list_threads")
        await peer.push(
            threads_frame(
                [
                    {
                        "platform": "cli", "thread_key": "cli:main",
                        "title": None, "status": "open",
                    },
                ]
            )
        )
        await _settle(app, lambda: bool(app.dropdown_options))

        n = len(peer.received)
        await pilot.press("escape")
        assert app.dropdown_options == []
        assert len(peer.received) == n


async def test_cancel_picker_with_no_cli_threads_says_so(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press(*"/cancel", "enter")
        await _settle(app, lambda: bool(peer.received) and
                      peer.received[-1].get("type") == "list_threads")
        await peer.push(
            threads_frame(
                [
                    {
                        "platform": "telegram", "thread_key": "-100:5",
                        "title": "chat", "status": "open",
                    },
                ]
            )
        )
        await _settle(
            app,
            lambda: any("nothing to cancel" in line.text for line in app.transcript),
        )
        assert app.dropdown_options == []


# ---- owner vs agent lines are visually distinct --------------------------------------


async def test_owner_lines_are_colored_and_spaced_apart_from_replies(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    """Owner echoes get their own color and a blank separator line above, so an
    exchange reads as a block and the agent's lines are tellable at a glance."""
    async with app.run_test() as pilot:
        await peer.push(reply_frame("cli:main", "earlier reply"))
        await _settle(
            app, lambda: any("earlier reply" in line.text for line in app.transcript)
        )
        await pilot.press(*"hi", "enter")
        await _settle(
            app, lambda: any(line.text == "> hi" for line in app.transcript)
        )
        owner_idx = next(
            i for i, line in enumerate(app.transcript) if line.text == "> hi"
        )
        assert app.transcript[owner_idx].style == OWNER_STYLE
        assert app.transcript[owner_idx - 1].text == ""  # breathing room above
        reply_line = next(
            line for line in app.transcript if line.text == "earlier reply"
        )
        assert reply_line.style != OWNER_STYLE


async def test_backfilled_owner_rows_use_the_owner_style(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await _switch_onto_telegram(app, peer, pilot)
        await peer.push(
            backfill_frame(
                "telegram",
                "-100:5",
                [
                    {
                        "role": "owner", "kind": "user", "text": "hi",
                        "filename": None, "created_at": "2026-01-01T00:00:00",
                    },
                    {
                        "role": "chief", "kind": "reply", "text": "hello",
                        "filename": None, "created_at": "2026-01-01T00:00:01",
                    },
                ],
            )
        )
        await _settle(app, lambda: any("hello" in line.text for line in app.transcript))
        owner_line = next(line for line in app.transcript if line.text == "> hi")
        assert owner_line.style == OWNER_STYLE
        reply_line = next(line for line in app.transcript if line.text == "hello")
        assert reply_line.style != OWNER_STYLE


# ---- ctrl+c: cancel the running turn, again to clear the chat -----------------------


async def test_ctrl_c_cancels_the_active_thread(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await pilot.press("ctrl+c")
        await _settle(app, lambda: bool(peer.received) and
                      peer.received[-1].get("type") == "command")
        assert peer.received[-1] == command_frame("cli:main", "cancel", "")
        assert app.is_running  # ctrl+c must not quit the app


async def test_second_ctrl_c_clears_the_chat(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await peer.push(reply_frame("cli:main", "old chatter"))
        await _settle(
            app, lambda: any("old chatter" in line.text for line in app.transcript)
        )
        await pilot.press("ctrl+c")
        await _settle(app, lambda: bool(peer.received) and
                      peer.received[-1].get("type") == "command")
        n = len(peer.received)
        await pilot.press("ctrl+c")
        await _settle(app, lambda: app.transcript == [])
        assert len(peer.received) == n  # the second press clears, it does not resend


async def test_typing_between_presses_disarms_the_clear(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    async with app.run_test() as pilot:
        await peer.push(reply_frame("cli:main", "keep me"))
        await _settle(
            app, lambda: any("keep me" in line.text for line in app.transcript)
        )
        await pilot.press("ctrl+c")
        await _settle(app, lambda: bool(peer.received) and
                      peer.received[-1].get("type") == "command")
        await pilot.press(*"x")
        await pilot.press("ctrl+c")
        await _settle(
            app, lambda: peer.received[-1] == command_frame("cli:main", "cancel", "")
        )
        assert any("keep me" in line.text for line in app.transcript)


async def test_ctrl_c_on_a_foreign_pane_sends_no_cancel(
    app: ChiefCliApp, peer: ScriptedPeer
) -> None:
    """A ``command`` frame dispatches on the cli engine — cancelling a foreign
    platform's thread through it is a no-op, so the press only arms the clear."""
    async with app.run_test() as pilot:
        await _switch_onto_telegram(app, peer, pilot)

        n = len(peer.received)
        await pilot.press("ctrl+c")
        await pilot.press("ctrl+c")
        await _settle(app, lambda: app.transcript == [])
        assert len(peer.received) == n


# --- The bottom edge is shared, so nothing may overlap it (#140 live-boot defect) -----
#
# Textual OVERLAYS widgets docked to the same edge, it does not stack them. With
# #statusbar, #prompt and Textual's own bottom-docked Footer all claiming the bottom,
# they landed on the same row: the input's last row was painted over by the statusbar
# and the footer, so the input bar rendered visibly cut off. Only the Footer docks now;
# the statusbar and prompt sit in normal flow above it and the transcript (1fr) absorbs
# the slack.


async def test_bottom_widgets_do_not_overlap(app: ChiefCliApp) -> None:
    async with app.run_test(size=(80, 24)):
        boxes = {
            name: app.query_one(f"#{name}").region
            for name in ("transcript", "statusbar", "prompt")
        }
        boxes["footer"] = app.query_one(Footer).region

        # Every widget is on screen, and no two of them share a row.
        for name, region in boxes.items():
            assert region.height > 0, f"{name} collapsed"
            assert region.y + region.height <= 24, f"{name} runs off the bottom"

        ordered = sorted(boxes.items(), key=lambda kv: kv[1].y)
        for (lo_name, lo), (hi_name, hi) in zip(ordered, ordered[1:], strict=False):
            assert lo.y + lo.height <= hi.y, (
                f"{lo_name} overlaps {hi_name}: "
                f"{lo_name} ends at {lo.y + lo.height}, {hi_name} starts at {hi.y}"
            )

        # The input keeps its full box — a cut-off prompt is the bug this pins.
        assert boxes["prompt"].height == 3
