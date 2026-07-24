"""Browser e2e capstone for the live cockpit (#268, PRD #260).

The one place the shipped JS (`/app.js`) is exercised for real: a real daemon
booted on localhost with only the LLM stubbed (the scripted ``FakeProvider``),
driven through Chromium. It proves the whole cockpit chain end to end —
real dispatcher → session → observer hub → SSE → the client rendering in the
browser — plus the two owner-facing controls (the stream-policy toggle and the
reply-from-web send guard).

Opt-in: marked ``browser`` (excluded from the default suite via pyproject's
``addopts``), run in its own CI job. Needs ``playwright install chromium``.
"""

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from playwright.async_api import Dialog, Page, async_playwright, expect

from chief.adapters.base import Adapter, Message
from chief.app import App, build_app
from chief.config import Config
from chief.policy import RICH
from chief.provider.base import Completion, ToolCall

from .fakes import FakeProvider, text_turn

pytestmark = [pytest.mark.browser, pytest.mark.timeout(120)]

PASSWORD = "e2e-owner-pass"
PEER = "+15551234567"  # an external 1:1 iMessage thread (a non-owner audience)


class _CapturingAdapter(Adapter):
    """A registered origin adapter that records its outbound sends — the fake
    device transport at the edge. The dispatch path feeding it is fully real."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[tuple[str, str]] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, thread_key: str, text: str) -> None:
        self.sent.append((thread_key, text))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait(pred: Callable[[], bool], timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def cockpit(
    tmp_path: Path, sock_path: Path
) -> AsyncIterator[tuple[App, str, _CapturingAdapter]]:
    """A live daemon serving the web UI on localhost, with a scripted model and
    a captured iMessage origin. Yields (app, base_url, imessage device)."""
    port = _free_port()
    # cli/imessage both stream RICH so a tapped browser sees deltas + tool ticks;
    # imessage's RICH send_guard drives the reply-from-web confirm on PEER.
    config = Config(
        models={"default": "test-model"},
        db_path=tmp_path / "chief.db",
        socket_path=sock_path,
        web_password=PASSWORD,
        web_host="127.0.0.1",
        web_port=port,
        stream_channel_defaults={"cli": RICH, "imessage": RICH},
    )
    provider = FakeProvider(
        [
            # cli:home wake — a read_file tool call, then the summary text.
            [Completion(
                text="",
                tool_calls=(ToolCall(
                    id="c1", name="read_file",
                    arguments={"path": "packages/build-imessage/manifest.yaml"},
                ),),
            )],
            text_turn("read the manifest"),
            text_turn("on my way"),   # the guarded reply-from-web send
            text_turn("will do"),     # the send after the guard is toggled off
        ]
    )
    app = await build_app(config, provider=provider)
    device = _CapturingAdapter("imessage")
    app.dispatcher.register(device)
    await app.start()
    await app.store.ensure_session("cli:home", "cli")
    await app.store.ensure_session(PEER, "imessage")
    base_url = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient() as probe:
        # Probe until uvicorn is accepting connections.
        for _ in range(300):
            try:
                if (await probe.get(base_url)).status_code == 200:
                    break
            except httpx.TransportError:
                await asyncio.sleep(0.05)
        else:
            raise AssertionError("web server never came up")
    try:
        yield app, base_url, device
    finally:
        await app.stop()


async def _login(page: Page, base_url: str) -> None:
    await page.goto(base_url)
    await page.fill("input[name=password]", PASSWORD)
    await page.click("button")
    await expect(page.locator("#input")).to_be_visible()


async def _focus(page: Page, app: App, thread: str) -> None:
    """Click a buffer and wait until its SSE tap is live on the server."""
    await page.locator("#buflist li", has_text=thread).first.click()
    await _wait(lambda: app.hub.is_watched(thread))


async def test_cockpit_live_mirror_tools_and_controls(
    cockpit: tuple[App, str, _CapturingAdapter],
) -> None:
    app, base_url, device = cockpit
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        dialogs: list[str] = []

        async def _on_dialog(dialog: Dialog) -> None:
            dialogs.append(dialog.message)
            await dialog.accept()

        page.on("dialog", _on_dialog)

        # AC: login.
        await _login(page, base_url)

        # AC: a non-web turn mirrors live into the focused buffer, its tool tick
        # renders, and load-result shows the args + output.
        await _focus(page, app, "cli:home")
        await app.dispatcher.handle(
            Message(channel="cli", sender="system",
                    thread_key="cli:home", text="ping")
        )
        log = page.locator("#log")
        await expect(log.locator(".msg.owner", has_text="ping")).to_be_visible()
        tool_row = log.locator(".msg.tool", has_text="read_file")
        await expect(tool_row).to_be_visible()
        await expect(
            log.locator(".msg.chief", has_text="read the manifest")
        ).to_be_visible()

        # Load-result: lazy-fetch the args + output off the row's click.
        await tool_row.get_by_role("button", name="load result").click()
        body = tool_row.locator(".toolbody")
        await expect(body).to_contain_text("args:")
        await expect(body).to_contain_text("build-imessage")

        # AC: the reply-from-web send guard confirms before a non-owner send.
        await _focus(page, app, PEER)
        await page.fill("#input", "you around?")
        await page.press("#input", "Enter")
        await _wait(lambda: len(device.sent) == 1)
        assert device.sent[0] == (PEER, "on my way")
        assert len(dialogs) == 1
        assert PEER in dialogs[0] and "imessage" in dialogs[0]

        # AC: a stream-policy toggle changes live behavior — turning the send
        # guard off means the next send dispatches with no confirm at all.
        await page.uncheck("#pol-guard")
        await _wait_guard_cleared(page)
        await page.fill("#input", "later then")
        await page.press("#input", "Enter")
        await _wait(lambda: len(device.sent) == 2)
        assert device.sent[1] == (PEER, "will do")
        assert len(dialogs) == 1  # no second confirm — the guard is off

        await browser.close()


async def _wait_guard_cleared(page: Page) -> None:
    """Wait until the client's cached row for the focused thread has dropped its
    send guard (the /policy POST + /sessions refresh have landed)."""
    for _ in range(500):
        guarded = await page.evaluate(
            "() => { const s = state.sessions.find("
            "x => x.thread === state.current); return s && s.send_guard; }"
        )
        if not guarded:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("send guard never cleared client-side")
