"""Opt-in real-browser end-to-end suite for the web UI (#153).

Skipped unless ``CHIEF_WEB_LIVE`` is set — the phone-flow demo in an actual
Chromium (headless, phone-sized viewport) against the FULL real stack from
``web_helpers``: real uvicorn on a real port, real SocketServer + CliAdapter +
TaskManager, real approval gate; the model stays the established fake seam. Mirrors
the live-suite pattern (``test_browser_live.py``): env-gated, deterministic, no
token spend.

Run it::

    uv run playwright install chromium   # once, downloads the pinned browser
    CHIEF_WEB_LIVE=1 uv run pytest tests/test_web_live.py
"""

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest.importorskip("playwright")
from playwright.async_api import Page, async_playwright  # noqa: E402

from chief.core.session import Delta  # noqa: E402
from test_cli_platform import _gate_factory, _seq_factory  # noqa: E402
from test_tasks import FakeSession  # noqa: E402
from web_helpers import PASSWORD, start_web_stack  # noqa: E402

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("CHIEF_WEB_LIVE"),
        reason="opt-in live browser suite: set CHIEF_WEB_LIVE=1",
    ),
    pytest.mark.timeout(120),
]

#: A phone, not a desktop — the layout must work here (hard requirement).
_PHONE_VIEWPORT = {"width": 390, "height": 844}


async def _login(page: Page, base: str) -> None:
    await page.goto(f"{base}/chat")
    await page.fill('input[name="password"]', PASSWORD)
    await page.click("button")
    await page.wait_for_selector(".composer")


async def test_phone_flow_login_chat_approve_files_settings(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    stack = await start_web_stack(
        tmp_path, session_factory, sdk_factory=_gate_factory(), with_gate=True
    )
    base = f"http://127.0.0.1:{stack.web.bound_port}"
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport=_PHONE_VIEWPORT)  # type: ignore[arg-type]

            # Login once; the persistent cookie keeps the tab live after that.
            await _login(page, base)

            # Chat: the gated turn raises a real approval card over SSE.
            await page.fill('input[name="text"]', "go")
            await page.click(".composer button")
            await page.wait_for_selector(".card", timeout=15_000)

            # Approve from the phone; the reply streams into the transcript.
            await page.click('.card button:has-text("Approve once")')
            await page.wait_for_selector(
                '.msg.chief:has-text("proceeded")', timeout=15_000
            )

            # Files: upload lands in the workspace and shows in the listing.
            await page.goto(f"{base}/files")
            upload = tmp_path / "from-phone.txt"
            upload.write_text("hello from the phone")
            await page.set_input_files('input[name="file"]', str(upload))
            await page.click('form[action="/files/upload"] button')
            await page.wait_for_selector('a:has-text("from-phone.txt")')

            # Settings: flip the LAN toggle; the flag lands in the config file.
            await page.goto(f"{base}/settings")
            await page.check('input[name="lan"]')
            await page.click('form[action="/settings/web"] button')
            await page.wait_for_url(f"{base}/settings")

            await browser.close()
    finally:
        await stack.aclose()


async def test_phone_slash_autocomplete_submits_command(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Typing ``/`` opens the palette; arrow-down + Enter runs the command."""
    stack = await start_web_stack(
        tmp_path,
        session_factory,
        sdk_factory=_seq_factory([FakeSession(model="m")]),
    )
    base = f"http://127.0.0.1:{stack.web.bound_port}"
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport=_PHONE_VIEWPORT)  # type: ignore[arg-type]
            await _login(page, base)

            composer = page.locator('.composer input[name="text"]')
            await composer.type("/")
            await page.wait_for_selector("#cmd-menu:not([hidden])")
            row = page.locator('.cmd-item:has-text("/tasks")')
            assert await row.count() == 1

            # /cancel is selected first; one ArrowDown lands on /tasks.
            await composer.press("ArrowDown")
            await composer.press("Enter")
            await page.wait_for_selector(
                '.msg.chief:has-text("No active tasks.")', timeout=15_000
            )
            assert await page.locator("#cmd-menu").is_hidden()

            await browser.close()
    finally:
        await stack.aclose()


async def test_phone_streaming_bubble_resolves_to_reply(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Deltas raise a draft bubble; the done-delta retires it for the reply."""
    gate = asyncio.Event()
    session = FakeSession(
        model="m",
        gate=gate,
        milestones=[
            Delta(message_id="m1", text="thinking "),
            Delta(message_id="m1", text="hard"),
        ],
        after_gate=[Delta(message_id="m1", text="", done=True)],
    )
    stack = await start_web_stack(
        tmp_path, session_factory, sdk_factory=_seq_factory([session])
    )
    base = f"http://127.0.0.1:{stack.web.bound_port}"
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport=_PHONE_VIEWPORT)  # type: ignore[arg-type]
            await _login(page, base)

            await page.fill('input[name="text"]', "go")
            await page.click(".composer button")

            # The draft bubble streams in while the turn is still open.
            await page.wait_for_selector(
                '.msg.streaming:has-text("thinking hard")', timeout=15_000
            )

            # Finish the turn: the draft retires and the real reply lands.
            gate.set()
            await page.wait_for_selector(
                '.msg.chief:has-text("reply:go")', timeout=15_000
            )
            assert await page.locator(".msg.streaming").count() == 0

            await browser.close()
    finally:
        await stack.aclose()
