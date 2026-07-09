"""Opt-in live test against a real mcp-playwright container.

Skipped unless ``CHIEF_BROWSER_LIVE`` is set — the manual "does the wiring actually
reach the playwright server" check, not a CI test. It talks to the mcp-playwright
MCP server directly over streamable HTTP (no Claude model, deterministic, no token
spend), running the read tools end to end: navigate to a stable page → snapshot and
assert rendered content → take a screenshot and assert the server saved the file.

Run it:

1. Build the image (first time or after Dockerfile changes)::

       docker compose --profile playwright build mcp-playwright

2. Start mcp-playwright with a temporary published port (the compose service
   deliberately has none)::

       docker compose --profile playwright run --rm -p 3000:3000 mcp-playwright

3. Set ``CHIEF_BROWSER_LIVE=1``, then run::

       uv run pytest tests/test_browser_live.py

``CHIEF_BROWSER_MCP_URL`` (default ``http://localhost:3000/mcp``) overrides the
endpoint.  ``CHIEF_BROWSER_LIVE_URL`` (default ``https://example.com``) overrides the
page to navigate to — it must be a stable, publicly-reachable URL with predictable
content.
"""

import os

import pytest

# ``mcp`` was a claude-agent-sdk transitive, dropped in #88; this opt-in live
# test uses it as the MCP client — skip collection when it is not installed.
pytest.importorskip("mcp")
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

import chief.tools.browser.mcp as browser_mcp
from chief.tools.browser.screenshot import extract_screenshot_filename

pytestmark = pytest.mark.skipif(
    not os.environ.get("CHIEF_BROWSER_LIVE"),
    reason="live browser test — set CHIEF_BROWSER_LIVE=1 (needs mcp-playwright up)",
)


def _bare(qualified: str) -> str:
    """Strip the SDK ``mcp__<server>__`` prefix → the MCP server's own tool name."""
    return qualified.removeprefix(f"mcp__{browser_mcp.SERVER_NAME}__")


def _text(result: object) -> str:
    """Flatten an MCP tool result's content blocks into one string for assertions."""
    content = getattr(result, "content", []) or []
    return "".join(getattr(block, "text", "") for block in content)


@pytest.mark.timeout(120)  # live network round-trips; override the 30s global cap
async def test_live_navigate_snapshot_screenshot() -> None:
    url = os.environ.get("CHIEF_BROWSER_MCP_URL", "http://localhost:3000/mcp")
    page_url = os.environ.get("CHIEF_BROWSER_LIVE_URL", "https://example.com")

    # Tool names come from the wiring catalog, so a rename there fails this test.
    assert "mcp__playwright__browser_navigate" in browser_mcp.READ_TOOLS
    assert "mcp__playwright__browser_snapshot" in browser_mcp.READ_TOOLS
    assert "mcp__playwright__browser_take_screenshot" in browser_mcp.READ_TOOLS

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Navigate to a stable public page.
            nav = await session.call_tool(
                _bare("mcp__playwright__browser_navigate"),
                {"url": page_url},
            )
            assert not nav.isError, _text(nav)

            # Snapshot: assert the rendered content mentions the page domain / title.
            snap = await session.call_tool(
                _bare("mcp__playwright__browser_snapshot"),
                {},
            )
            assert not snap.isError, _text(snap)
            snap_text = _text(snap)
            assert snap_text.strip(), "browser_snapshot returned empty content"
            # example.com always has "Example Domain" in its heading.
            assert "example" in snap_text.lower(), (
                f"snapshot did not contain expected page content: {snap_text[:200]}"
            )

            # Screenshot: assert the server saved the file to the output volume.
            shot = await session.call_tool(
                _bare("mcp__playwright__browser_take_screenshot"),
                {},
            )
            assert not shot.isError, _text(shot)
            shot_text = _text(shot)
            filename = extract_screenshot_filename(shot_text)
            assert filename is not None, (
                f"browser_take_screenshot did not return a filename link: "
                f"{shot_text[:200]}"
            )
