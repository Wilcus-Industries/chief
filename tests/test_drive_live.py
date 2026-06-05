"""Opt-in live test against real Google Drive through a running mcp-drive.

Skipped unless ``CHIEF_DRIVE_LIVE`` is set — the manual "does the wiring actually reach
Google" check, not a CI test. It talks to chief's own drive MCP server directly over
streamable HTTP (no Claude model, deterministic, no token spend), exercising the
read tool (``ReadDriveFile``) against a real Drive file.

The write tool (``UploadMarkdownAsPDF``) is *not* covered here: it reads ``file_path``
from the mcp-drive container's own filesystem (docker/mcp-drive/server.py), which a
host-side pytest can't stage — exercise it through the full bot path instead.

Run it:

1. Mint the shared token:  ``python -m chief.tools.google.auth``
2. Start mcp-drive with a temporary published port (the compose service deliberately
   has none):  ``docker compose --profile google run --rm -p 8001:8001 mcp-drive``
3. Set ``CHIEF_DRIVE_LIVE=1`` and ``CHIEF_DRIVE_TEST_FILE_URL=<doc/pdf/office url>``,
   then run ``uv run pytest tests/test_drive_live.py``.

``CHIEF_DRIVE_TEST_FILE_URL`` must point at a readable Google Doc, PDF, or Office file
(the read is non-destructive). ``CHIEF_DRIVE_MCP_URL`` (default
``http://localhost:8001/mcp``) overrides the endpoint.
"""

import os

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import chief.tools.drive.mcp as drive_mcp

pytestmark = pytest.mark.skipif(
    not os.environ.get("CHIEF_DRIVE_LIVE"),
    reason="live drive test — set CHIEF_DRIVE_LIVE=1 (needs mcp-drive up)",
)


def _bare(qualified: str) -> str:
    """Strip the SDK ``mcp__<server>__`` prefix → the MCP server's own tool name."""
    return qualified.removeprefix(f"mcp__{drive_mcp.SERVER_NAME}__")


def _text(result: object) -> str:
    """Flatten an MCP tool result's content blocks into one string for assertions."""
    content = getattr(result, "content", []) or []
    return "".join(getattr(block, "text", "") for block in content)


@pytest.mark.timeout(120)  # live network round-trips; override the 30s global cap
async def test_live_read_drive_file() -> None:
    file_url = os.environ.get("CHIEF_DRIVE_TEST_FILE_URL")
    if not file_url:
        pytest.skip("set CHIEF_DRIVE_TEST_FILE_URL to a readable Doc/PDF/Office url")
    url = os.environ.get("CHIEF_DRIVE_MCP_URL", "http://localhost:8001/mcp")

    # Tool names come from the wiring catalog, so a rename there fails this test.
    assert "mcp__drive__ReadDriveFile" in drive_mcp.READ_TOOLS
    assert "mcp__drive__UploadMarkdownAsPDF" in drive_mcp.WRITE_TOOLS

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                _bare("mcp__drive__ReadDriveFile"),
                {"url": file_url},
            )
            assert not result.isError, _text(result)
            assert _text(result).strip(), "ReadDriveFile returned empty text"
