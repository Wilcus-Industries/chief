"""Opt-in live test against a real Google Sheet through a running mcp-sheets.

Skipped unless ``CHIEF_SHEETS_LIVE`` is set — the manual "does the wiring actually reach
Google" check, not a CI test. It talks to chief's own sheets MCP server directly over
streamable HTTP (no Claude model, deterministic, no token spend), running read tools
(``list_sheets`` → ``get_sheet_data``) plus a real ``update_cells`` write — and asserts
the **server-side row-1 (header) guard** blocks a write to ``A1``.

Non-destructive: it only touches ``B2`` (a scratch cell), captures its original value
first, and restores it in a ``finally``. Point it at a *throwaway* spreadsheet anyway.

Run it:

1. Mint the shared token:  ``python -m chief.tools.google.auth``
2. Start mcp-sheets with a temporary published port (the compose service deliberately
   has none):  ``docker compose --profile google run --rm -p 8002:8002 mcp-sheets``
3. Set ``CHIEF_SHEETS_LIVE=1`` and ``CHIEF_SHEETS_TEST_SPREADSHEET=<throwaway-id>``,
   then run ``uv run pytest tests/test_sheets_live.py``.

``CHIEF_SHEETS_TEST_SPREADSHEET`` is the spreadsheet id (from its URL).
``CHIEF_SHEETS_MCP_URL`` (default ``http://localhost:8002/mcp``) overrides the endpoint.
"""

import os
import secrets
from typing import Any

import pytest

# ``mcp`` was a claude-agent-sdk transitive, dropped in #88; this opt-in live
# test uses it as the MCP client — skip collection when it is not installed.
pytest.importorskip("mcp")
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

import chief.tools.sheets.mcp as sheets_mcp

pytestmark = pytest.mark.skipif(
    not os.environ.get("CHIEF_SHEETS_LIVE"),
    reason="live sheets test — set CHIEF_SHEETS_LIVE=1 (needs mcp-sheets up)",
)

SCRATCH_CELL = "B2"  # row >= 2: the row-1 guard leaves it alone
ROW_1_PHRASE = "cannot edit the first row"  # from docker/mcp-sheets/row1_guard.py


def _bare(qualified: str) -> str:
    """Strip the SDK ``mcp__<server>__`` prefix → the MCP server's own tool name."""
    return qualified.removeprefix(f"mcp__{sheets_mcp.SERVER_NAME}__")


def _text(result: object) -> str:
    """Flatten an MCP tool result's content blocks into one string for assertions."""
    content = getattr(result, "content", []) or []
    return "".join(getattr(block, "text", "") for block in content)


def _result(result: object) -> Any:
    """The tool's structured return payload (FastMCP wraps it under ``result``)."""
    structured = getattr(result, "structuredContent", None) or {}
    return structured.get("result")


def _first_cell(result: object) -> str:
    """The top-left cell of a ``get_sheet_data`` result, or "" if the range is empty.

    The tool returns ``{... "valueRanges": [{"range": ..., "values": [[cell]]}]}``; an
    empty range yields ``"values": []``. Tolerant — any miss reads as empty so the
    restore clears the cell.
    """
    payload = _result(result) or {}
    ranges = payload.get("valueRanges") or []
    values = (ranges[0].get("values") if ranges else None) or []
    return str(values[0][0]) if values and values[0] else ""


@pytest.mark.timeout(120)  # live network round-trips; override the 30s global cap
async def test_live_read_write_and_row1_guard() -> None:
    spreadsheet_id = os.environ.get("CHIEF_SHEETS_TEST_SPREADSHEET")
    if not spreadsheet_id:
        pytest.skip("set CHIEF_SHEETS_TEST_SPREADSHEET to a throwaway spreadsheet id")
    url = os.environ.get("CHIEF_SHEETS_MCP_URL", "http://localhost:8002/mcp")
    marker = "chief-live-" + secrets.token_hex(6)

    # Tool names come from the wiring catalog, so a rename there fails this test.
    assert "mcp__sheets__list_sheets" in sheets_mcp.READ_TOOLS
    assert "mcp__sheets__get_sheet_data" in sheets_mcp.READ_TOOLS
    assert "mcp__sheets__update_cells" in sheets_mcp.WRITE_TOOLS

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            listed = await session.call_tool(
                _bare("mcp__sheets__list_sheets"),
                {"spreadsheet_id": spreadsheet_id},
            )
            assert not listed.isError, _text(listed)
            sheets = _result(listed)
            assert sheets, _text(listed)  # readable failure if the shape ever changes
            sheet = sheets[0]  # first tab name, e.g. "Sheet1"

            read_args = {
                "spreadsheet_id": spreadsheet_id,
                "sheet": sheet,
                "range": SCRATCH_CELL,
            }
            original = _first_cell(
                await session.call_tool(_bare("mcp__sheets__get_sheet_data"), read_args)
            )
            try:
                # Write a marker to the scratch cell (row >= 2 → guard allows).
                wrote = await session.call_tool(
                    _bare("mcp__sheets__update_cells"),
                    {**read_args, "data": [[marker]]},
                )
                assert not wrote.isError, _text(wrote)

                # Read it back — the write reached Google and round-tripped.
                back = await session.call_tool(
                    _bare("mcp__sheets__get_sheet_data"), read_args
                )
                assert not back.isError, _text(back)
                assert _first_cell(back) == marker, _text(back)

                # Row-1 guard: a write to A1 must be refused server-side.
                blocked = await session.call_tool(
                    _bare("mcp__sheets__update_cells"),
                    {
                        "spreadsheet_id": spreadsheet_id,
                        "sheet": sheet,
                        "range": "A1",
                        "data": [["chief-should-not-land"]],
                    },
                )
                assert blocked.isError, "row-1 write was NOT blocked"
                assert ROW_1_PHRASE in _text(blocked).lower(), _text(blocked)
            finally:
                # Best-effort restore the scratch cell to its original value
                # (stringified — lossy for a pre-existing numeric/bool cell, hence
                # the docstring's "throwaway spreadsheet").
                try:
                    await session.call_tool(
                        _bare("mcp__sheets__update_cells"),
                        {**read_args, "data": [[original]]},
                    )
                except Exception:  # cleanup is best-effort
                    pass
