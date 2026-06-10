"""The ``mcp-drive`` server: tool catalog + the SDK ``mcp_servers`` entry.

chief's own FastMCP Drive server (``docker/mcp-drive``, ported from genesis-x) runs as
its own container, streamable HTTP on port 8001, MCP mounted at ``/mcp``. Two tools:
``ReadDriveFile`` (read a Doc/PDF/Office file from a Drive URL) and
``UploadMarkdownAsPDF`` (render a local Markdown file to PDF and upload it to a folder).

Wiring rules (DESIGN: reads ALLOW, writes ASK): the read is pre-approved; the upload is
a write, so it stays out of ``allowed_tools`` and reaches the owner's approval card.
"""

from ..google import GoogleService, qualified

#: The SDK names an MCP tool ``mcp__<server>__<tool>``; this is the ``<server>`` half.
SERVER_NAME = "drive"

#: Read-only Drive tool — the gate ALLOWs it with no approval card.
READ_TOOLS: tuple[str, ...] = qualified(SERVER_NAME, "ReadDriveFile")

#: Effectful Drive tool — wired for the owner but routed through ASK → approval.
WRITE_TOOLS: tuple[str, ...] = qualified(SERVER_NAME, "UploadMarkdownAsPDF")

#: Nothing hard-blocked for Drive (the upload is approval-gated, not deferred).
DEFERRED_TOOLS: tuple[str, ...] = ()


def service(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> GoogleService:
    """The :class:`GoogleService` for the drive container at ``url``.

    ``headers`` is forwarded to every HTTP call the SDK makes to the MCP server
    — used to inject ``X-Account-Label`` for multi-account credential selection
    (issue #47).  ``None`` → no extra headers (single-account / no binding).
    """
    return GoogleService(
        name="drive",
        server_name=SERVER_NAME,
        url=url,
        read_tools=READ_TOOLS,
        write_tools=WRITE_TOOLS,
        deferred_tools=DEFERRED_TOOLS,
        headers=headers,
    )
