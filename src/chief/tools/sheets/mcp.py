"""The ``mcp-sheets`` server: tool catalog + the SDK ``mcp_servers`` entry.

chief wraps the third-party ``xing5/mcp-google-sheets`` (pinned ``0.6.3``) in its own
container (``docker/mcp-sheets``), streamable HTTP on port 8002, MCP at ``/mcp``,
with a server-side guard that refuses edits to row 1 (the header row). This module names
the package's tools and partitions them for the gate.

Wiring rules (DESIGN: reads ALLOW, writes ASK): listings / range + formula reads are
pre-approved; cell/row/sheet writes and ``share_spreadsheet`` stay out of
``allowed_tools`` so each reaches the owner's approval card (row-1 writes are also
blocked server-side).
"""

from ..google import GoogleService, qualified

#: The SDK names an MCP tool ``mcp__<server>__<tool>``; this is the ``<server>`` half.
SERVER_NAME = "sheets"

#: Read-only sheet tools — the gate ALLOWs these with no approval card. Names track
#: ``mcp-google-sheets`` 0.6.3 (pinned in docker/mcp-sheets/requirements.txt).
READ_TOOLS: tuple[str, ...] = qualified(
    SERVER_NAME,
    "get_sheet_data",
    "get_sheet_formulas",
    "list_sheets",
    "get_multiple_sheet_data",
    "get_multiple_spreadsheet_summary",
    "list_spreadsheets",
    "list_folders",
    "search_spreadsheets",
    "find_in_spreadsheet",
)

#: Effectful sheet tools — wired for the owner but routed through ASK → approval.
#: ``share_spreadsheet`` grants others access, so it is approval-gated like a write.
WRITE_TOOLS: tuple[str, ...] = qualified(
    SERVER_NAME,
    "update_cells",
    "batch_update_cells",
    "add_rows",
    "add_columns",
    "copy_sheet",
    "rename_sheet",
    "create_spreadsheet",
    "create_sheet",
    "share_spreadsheet",
    "batch_update",
    "add_chart",
)

#: Nothing hard-blocked for Sheets — writes are approval-gated, and row 1 is guarded
#: server-side (the guard lives in docker/mcp-sheets/server.py).
DEFERRED_TOOLS: tuple[str, ...] = ()


def service(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> GoogleService:
    """The :class:`GoogleService` for the sheets container at ``url``.

    ``headers`` is forwarded to every HTTP call the SDK makes to the MCP server
    — used to inject ``X-Account-Label`` for multi-account credential selection
    (issue #47).  ``None`` → no extra headers (single-account / no binding).
    """
    return GoogleService(
        name="sheets",
        server_name=SERVER_NAME,
        url=url,
        read_tools=READ_TOOLS,
        write_tools=WRITE_TOOLS,
        deferred_tools=DEFERRED_TOOLS,
        headers=headers,
    )
