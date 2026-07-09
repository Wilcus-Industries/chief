"""Shared Google MCP wiring: the per-service tool-catalog shape.

chief runs its **own** Google MCP servers — calendar, drive, sheets — each a Python
FastMCP server in its own container over streamable HTTP (``docker/mcp-*``), reached by
core on the internal compose network. A :class:`GoogleService` bundles one server's URL
with its tool partitions (read / write / deferred), so the task engine and gate wire any
number of services through one uniform shape instead of per-service literals.

Each service's catalog module (``tools/<svc>/mcp.py``) exposes a ``service(url)``
factory returning one of these. The partitions drive the gate (reads ALLOW, writes ASK,
deferred hard-blocked):

- :attr:`read_tools` — pre-approved: listed in ``allowed_tools`` *and* fed to the gate
  as extra read-only names, so the ``PreToolUse`` hook allows them with no card.
- :attr:`write_tools` — kept **out** of ``allowed_tools`` so the SDK routes them to
  ``can_use_tool`` → the owner's approval card.
- :attr:`deferred_tools` — placed in ``disallowed_tools``; the model can't call them.
"""

from dataclasses import dataclass

from ...gate.types import McpHttpServerConfig


def qualified(server_name: str, *tools: str) -> tuple[str, ...]:
    """SDK-qualify bare MCP tool names as ``mcp__<server>__<tool>``."""
    return tuple(f"mcp__{server_name}__{tool}" for tool in tools)


@dataclass(frozen=True)
class GoogleService:
    """One Google MCP server's compose URL + its read/write/deferred tool names."""

    name: str
    server_name: str
    url: str
    read_tools: tuple[str, ...]
    write_tools: tuple[str, ...]
    deferred_tools: tuple[str, ...] = ()
    #: Optional per-session HTTP headers injected into every MCP call to this
    #: server (e.g. ``{"X-Account-Label": "work@corp.com"}`` for multi-account
    #: credential selection, issue #46).  ``None`` or empty → no extra headers.
    headers: dict[str, str] | None = None

    def server_config(self) -> McpHttpServerConfig:
        """The ``mcp_servers`` entry for this server (streamable HTTP)."""
        config: McpHttpServerConfig = {"type": "http", "url": self.url}
        if self.headers:
            config["headers"] = dict(self.headers)
        return config
