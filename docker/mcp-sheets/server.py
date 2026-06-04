"""Google Sheets MCP server — third-party ``mcp-google-sheets`` wrapped for chief.

We don't reimplement Sheets; we import the pinned ``xing5/mcp-google-sheets`` FastMCP
instance and wrap it (the row-1 guard is ported from genesis-x's ``sheets/server.py``):

- a server-side guard refuses any edit that touches row 1 (the header row), so a
  bad header write is blocked even before chief's approval gate — it monkeypatches the
  tool manager's ``call_tool`` to inspect the A1 range / start-row of write tools,
- transport is set to ``streamable-http`` (chief reaches it at ``/mcp``) and a
  ``/health`` route is added for the compose healthcheck,
- host/port are pinned for the container.

Auth is the package's own: it reads ``CREDENTIALS_PATH`` (the OAuth client) and
``TOKEN_PATH`` (the shared minted token) from the environment. This container is the
sole writer of the shared token file. Standalone image — no chief import.
"""

import os
import re
from typing import Any

from mcp.types import TextContent
from mcp_google_sheets.server import mcp

PORT = int(os.environ.get("PORT", "8002"))

ROW_1_ERROR = (
    "You cannot edit the first row of a Google Sheet. "
    "Ask a human to perform this edit for you if it was intentional."
)


def _a1_includes_row_1(range_str: str) -> bool:
    s = range_str.split("!")[-1].strip().upper()
    # Whole-row notation: "1:5" or "1:1".
    if re.match(r"^\d+:\d+$", s):
        return int(s.split(":")[0]) == 1
    # Standard cell/range notation: parse the start row number.
    m = re.match(r"^[A-Z]*(\d+)", s)
    if m:
        return int(m.group(1)) == 1
    # No row number (e.g. "A:B" — whole column, implicitly includes row 1).
    return True


def _check_row_1(tool_name: str, arguments: dict[str, Any]) -> str | None:
    if tool_name == "update_cells":
        r = arguments.get("range", "")
        if r and _a1_includes_row_1(r):
            return ROW_1_ERROR

    elif tool_name == "batch_update_cells":
        ranges = arguments.get("ranges", {})
        if any(_a1_includes_row_1(k) for k in ranges):
            return ROW_1_ERROR

    elif tool_name == "add_rows":
        start = arguments.get("start_row")
        if start is None or start == 0:
            return ROW_1_ERROR

    elif tool_name == "add_chart":
        r = arguments.get("data_range", "")
        if r and _a1_includes_row_1(r):
            return ROW_1_ERROR

    elif tool_name == "batch_update":
        for req in arguments.get("requests", []):
            uc = req.get("updateCells", {})
            if uc.get("range", {}).get("startRowIndex", -1) == 0:
                return ROW_1_ERROR
            ins = req.get("insertDimension", {})
            rng = ins.get("range", {})
            if rng.get("dimension") == "ROWS" and rng.get("startIndex", -1) == 0:
                return ROW_1_ERROR

    return None


_original_call_tool = mcp._tool_manager.call_tool


async def _guarded_call_tool(
    name: str, arguments: dict[str, Any], **kwargs: Any
) -> Any:
    err = _check_row_1(name, arguments)
    if err:
        return [TextContent(type="text", text=err)]
    return await _original_call_tool(name, arguments, **kwargs)


mcp._tool_manager.call_tool = _guarded_call_tool  # type: ignore[method-assign]

mcp.settings.host = "0.0.0.0"
mcp.settings.port = PORT


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Any) -> Any:
    """Liveness probe for the compose healthcheck."""
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
