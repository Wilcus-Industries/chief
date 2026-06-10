"""The ``mcp-playwright`` server: tool catalog + the SDK ``mcp_servers`` entry.

The stock ``@playwright/mcp`` package (pinned ``0.0.76``) runs in its own container
(``docker/mcp-playwright``), headless Chromium, streamable HTTP on port 3000, MCP at
``/mcp``. Core reaches it over the compose network. This module names the server's tool
names so the gate and session layers stay decoupled from the specific package version.

Tool names are the native ``browser_*`` names exposed by ``@playwright/mcp`` 0.0.76;
the SDK qualifies them as ``mcp__playwright__browser_*``.

Wiring rules (reads ALLOW, writes ASK):

- :data:`READ_TOOLS` — navigation, page inspection, screenshot, console/network
  observation, waits, and tab listing: pre-approved, no approval card.
- :data:`WRITE_TOOLS` — interaction (click, type, fill, select, drag, upload),
  dialogs, and arbitrary-JS tools (``browser_evaluate``, ``browser_run_code_unsafe``):
  absent from ``allowed_tools``, so each reaches the owner's approval card.

The arbitrary-JS tools are in :data:`WRITE_TOOLS` by design — executing arbitrary
JavaScript inside a page is an effectful, potentially irreversible action.

Browser tools are **owner-only**: guest sessions never receive the playwright MCP server
(tier isolation by construction, same as Google and shell services).
"""

from ..google import GoogleService, qualified

#: The SDK names an MCP tool ``mcp__<server>__<tool>``; this is the ``<server>`` half.
#: Matches the service name given in the ``mcp-playwright`` compose entry.
SERVER_NAME = "playwright"

#: Read-only browser tools — navigation, inspection, screenshot, waits, tab listing.
#: The gate ALLOWs these with no approval card.
#: Tool names track ``@playwright/mcp`` 0.0.76 (pinned in docker/mcp-playwright).
READ_TOOLS: tuple[str, ...] = qualified(
    SERVER_NAME,
    "browser_navigate",
    "browser_navigate_back",
    "browser_navigate_forward",
    "browser_reload",
    "browser_snapshot",
    "browser_take_screenshot",
    "browser_console_messages",
    "browser_network_requests",
    "browser_wait_for",
    "browser_tabs",
)

#: Effectful browser tools — interaction, dialogs, and arbitrary-JS execution.
#: Absent from ``allowed_tools`` so each reaches the owner's approval card.
#: ``browser_evaluate`` and ``browser_run_code_unsafe`` sit here (arbitrary JS).
WRITE_TOOLS: tuple[str, ...] = qualified(
    SERVER_NAME,
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_check",
    "browser_uncheck",
    "browser_drag",
    "browser_drop",
    "browser_file_upload",
    "browser_handle_dialog",
    "browser_press_key",
    "browser_press_sequentially",
    "browser_hover",
    "browser_mouse_click_xy",
    "browser_mouse_drag_xy",
    "browser_mouse_down",
    "browser_mouse_up",
    "browser_mouse_move_xy",
    "browser_mouse_wheel",
    "browser_keydown",
    "browser_keyup",
    "browser_evaluate",
    "browser_run_code_unsafe",
    "browser_close",
    "browser_resize",
)

#: Nothing hard-blocked for the browser — writes are approval-gated, no deferred ops.
DEFERRED_TOOLS: tuple[str, ...] = ()


def service(url: str) -> GoogleService:
    """The :class:`~chief.tools.google.GoogleService` for the playwright container."""
    return GoogleService(
        name="browser",
        server_name=SERVER_NAME,
        url=url,
        read_tools=READ_TOOLS,
        write_tools=WRITE_TOOLS,
        deferred_tools=DEFERRED_TOOLS,
    )
