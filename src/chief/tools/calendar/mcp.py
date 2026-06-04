"""The ``mcp-gcal`` server: tool catalog + the SDK ``mcp_servers`` entry.

``nspady/google-calendar-mcp`` runs as its own container (Streamable HTTP, port 3000,
MCP at the root path) — chief reaches it over the internal compose network. This module
is the one place that knows the image's tool names, so the gate and session layers stay
decoupled from the specific MCP (they take these constants, never literals).

Wiring rules (DESIGN: reads ALLOW, writes ASK):

- :data:`READ_TOOLS` — pre-approved: listed in ``allowed_tools`` *and* fed to the gate
  as extra read-only names, so the ``PreToolUse`` hook allows them with no card.
- :data:`WRITE_TOOLS` — create/update: deliberately **absent** from ``allowed_tools`` so
  the SDK routes them to ``can_use_tool`` → the owner's approval card.
- :data:`DEFERRED_TOOLS` — delete / batch-create / RSVP: put in ``disallowed_tools`` so
  the model can't call them at all. Cancellation is a documented M5+ follow-up.
"""

from claude_agent_sdk.types import McpHttpServerConfig

#: The SDK names an MCP tool ``mcp__<server>__<tool>``; this is the ``<server>`` half.
SERVER_NAME = "gcal"


def _qualified(*tools: str) -> tuple[str, ...]:
    return tuple(f"mcp__{SERVER_NAME}__{tool}" for tool in tools)


#: Read-only calendar tools — the gate ALLOWs these with no approval card.
READ_TOOLS: tuple[str, ...] = _qualified(
    "list-calendars",
    "list-events",
    "search-events",
    "get-event",
    "get-freebusy",
    "list-colors",
    "get-current-time",
)

#: Effectful calendar tools — wired for the owner but routed through ASK → approval.
WRITE_TOOLS: tuple[str, ...] = _qualified("create-event", "update-event")

#: Out of M5 scope — hard-blocked via ``disallowed_tools``. ``delete-event`` is the
#: deferred cancellation flow (DESIGN M5 note); the others are batch/RSVP extras.
DEFERRED_TOOLS: tuple[str, ...] = _qualified(
    "delete-event",
    "create-events",
    "respond-to-event",
)

# Note: writes are intentionally *not* collected into an "allowed" list. They are
# available to the owner via the wired MCP server but kept OUT of the SDK
# ``allowed_tools`` so each create/update reaches ``can_use_tool`` → approval.


def server_config(url: str) -> McpHttpServerConfig:
    """The ``mcp_servers`` entry for the calendar container at ``url``."""
    return {"type": "http", "url": url}
