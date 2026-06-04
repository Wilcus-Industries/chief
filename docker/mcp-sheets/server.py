"""Google Sheets MCP server — third-party ``mcp-google-sheets`` wrapped for chief.

We don't reimplement Sheets; we import the pinned ``xing5/mcp-google-sheets`` FastMCP
instance and wrap it (the row-1 guard is ported from genesis-x's ``sheets/server.py``):

- a server-side guard (``row1_guard``) refuses any edit that touches row 1 (the header
  row), so a bad header write is blocked even before chief's approval gate — it
  monkeypatches the tool manager's ``call_tool`` to inspect the A1 range of write tools,
- transport is set to ``streamable-http`` (chief reaches it at ``/mcp``) and a
  ``/health`` route is added for the compose healthcheck,
- host/port are pinned for the container.

Auth is the package's own: it reads ``CREDENTIALS_PATH`` (the OAuth client) and
``TOKEN_PATH`` (the shared minted token) from the environment. This container is the
sole writer of the shared token file. Standalone image — no chief import.
"""

import os
from typing import Any

from mcp.types import TextContent
from mcp_google_sheets.server import mcp
from row1_guard import _check_row_1

PORT = int(os.environ.get("PORT", "8002"))


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
