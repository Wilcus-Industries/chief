"""Screenshot harness for visual passes over the web UI theme.

Not an assertion suite — gated behind ``CHIEF_WEB_SHOTS`` so a plain ``uv run
pytest`` never runs it. Drives the same full stack as ``test_web_live.py`` and
captures every page at the phone viewport in both color schemes, for eyeballing
theme changes before they ship::

    CHIEF_WEB_SHOTS=1 CHIEF_SHOTS_DIR=/tmp/shots uv run pytest tests/test_web_shots.py
"""

import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest.importorskip("playwright")
from playwright.async_api import async_playwright  # noqa: E402

from chief.core.session import Milestone, ToolEnd, ToolStart  # noqa: E402
from test_cli_platform import _seq_factory  # noqa: E402
from test_tasks import FakeSession  # noqa: E402
from web_helpers import PASSWORD, start_web_stack  # noqa: E402

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("CHIEF_WEB_SHOTS"),
        reason="screenshot harness: set CHIEF_WEB_SHOTS=1",
    ),
    pytest.mark.timeout(180),
]

_PHONE_VIEWPORT = {"width": 390, "height": 844}


async def test_capture_shots(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    out = Path(os.environ["CHIEF_SHOTS_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    session = FakeSession(
        model="m",
        milestones=[
            ToolStart(tool_call_id="t1", name="Bash"),
            ToolEnd(tool_call_id="t1", ok=True),
            Milestone(text="using Bash"),
            ToolStart(tool_call_id="t2", name="Edit"),
            ToolEnd(tool_call_id="t2", ok=False, detail="file not found"),
        ],
    )
    stack = await start_web_stack(
        tmp_path, session_factory, sdk_factory=_seq_factory([session])
    )
    base = f"http://127.0.0.1:{stack.web.bound_port}"
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            for scheme in ("dark", "light"):
                page = await browser.new_page(viewport=_PHONE_VIEWPORT)  # type: ignore[arg-type]
                await page.emulate_media(color_scheme=scheme)

                await page.goto(f"{base}/chat")
                await page.screenshot(path=str(out / f"login-{scheme}.png"))
                await page.fill('input[name="password"]', PASSWORD)
                await page.click("button")
                await page.wait_for_selector(".composer")

                if scheme == "dark":  # one turn total; second scheme just re-renders
                    await page.fill('input[name="text"]', "hello chief")
                    await page.click(".composer button")
                    await page.wait_for_selector(
                        '.msg.chief:has-text("reply:")', timeout=15_000
                    )
                else:
                    await page.goto(f"{base}/chat")
                    await page.wait_for_selector(".msg.chief")
                await page.screenshot(path=str(out / f"chat-{scheme}.png"))

                await page.fill('input[name="text"]', "/")
                await page.wait_for_selector("#cmd-menu .cmd-item")
                await page.screenshot(path=str(out / f"chat-menu-{scheme}.png"))

                await page.goto(f"{base}/files")
                if scheme == "dark":
                    upload = tmp_path / "notes.txt"
                    upload.write_text("hello")
                    await page.set_input_files('input[name="file"]', str(upload))
                    await page.click('form[action="/files/upload"] button')
                    await page.wait_for_selector('a:has-text("notes.txt")')
                await page.screenshot(path=str(out / f"files-{scheme}.png"))

                await page.goto(f"{base}/settings")
                await page.screenshot(
                    path=str(out / f"settings-{scheme}.png"), full_page=True
                )

                await page.goto(f"{base}/health")
                await page.screenshot(path=str(out / f"health-{scheme}.png"))
                await page.close()
            await browser.close()
    finally:
        await stack.aclose()
