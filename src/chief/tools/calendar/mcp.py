"""The ``mcp-calendar`` server: tool catalog + the SDK ``mcp_servers`` entry.

chief's own FastMCP calendar server (``docker/mcp-calendar``) runs as its own container,
streamable HTTP on port 8003, MCP mounted at ``/mcp`` — core reaches it over the compose
network. This module is the one place that knows the server's tool names, so the gate
and session layers stay decoupled from the specific MCP (they take these constants).

Wiring rules (DESIGN: reads ALLOW, writes ASK):

- :data:`READ_TOOLS` — pre-approved: listed in ``allowed_tools`` *and* fed to the gate
  as extra read-only names, so the ``PreToolUse`` hook allows them with no card.
- :data:`WRITE_TOOLS` — create/update: deliberately **absent** from ``allowed_tools`` so
  the SDK routes them to ``can_use_tool`` → the owner's approval card.
- :data:`DEFERRED_TOOLS` — delete: in ``disallowed_tools`` so the model can't call it.
  Cancellation is a documented follow-up.
"""

from ..google import GoogleService, qualified

#: The SDK names an MCP tool ``mcp__<server>__<tool>``; this is the ``<server>`` half.
SERVER_NAME = "calendar"

#: Read-only calendar tools — the gate ALLOWs these with no approval card.
READ_TOOLS: tuple[str, ...] = qualified(
    SERVER_NAME,
    "list-calendars",
    "list-events",
    "get-event",
    "get-freebusy",
    "get-current-time",
)

#: Effectful calendar tools — wired for the owner but routed through ASK → approval.
WRITE_TOOLS: tuple[str, ...] = qualified(SERVER_NAME, "create-event", "update-event")

#: Hard-blocked via ``disallowed_tools``. ``delete-event`` is the deferred cancellation
#: flow — the server implements it (so the live test can clean up) but chief blocks it.
DEFERRED_TOOLS: tuple[str, ...] = qualified(SERVER_NAME, "delete-event")


def service(url: str) -> GoogleService:
    """The :class:`GoogleService` for the calendar container at ``url``."""
    return GoogleService(
        name="calendar",
        server_name=SERVER_NAME,
        url=url,
        read_tools=READ_TOOLS,
        write_tools=WRITE_TOOLS,
        deferred_tools=DEFERRED_TOOLS,
    )
