"""Self-added MCP servers: the agent wires in new servers behind the gate.

Approved additions persist to a data file (loaded again at boot) and land
in the audit log.
"""

import json
import logging
from pathlib import Path
from typing import Any

from chief.agent.tools import Tool, ToolRegistry
from chief.audit import AuditLog
from chief.mcpclient.manager import McpManager, ServerConfig
from chief.provider.base import ToolSpec

logger = logging.getLogger(__name__)

SELF_ADDED_FILE = Path("data/mcp_servers.json")

_ADD_SPEC = ToolSpec(
    name="add_mcp_server",
    description=(
        "Wire in a new MCP tool server. Give exactly one of `url` (HTTP) or "
        "`command` (stdio child process argv). The server's tools become "
        "available as mcp_<name>_<tool>. Persisted across restarts."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "url": {"type": "string"},
            "command": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
        },
        "required": ["name", "rationale"],
    },
)


def load_self_added(path: Path = SELF_ADDED_FILE) -> list[ServerConfig]:
    """Servers the agent added in earlier sessions."""
    if not path.exists():
        return []
    entries = json.loads(path.read_text())
    return [
        ServerConfig(
            name=e["name"],
            url=e.get("url"),
            command=tuple(e["command"]) if e.get("command") else None,
        )
        for e in entries
    ]


def _persist(config: ServerConfig, path: Path) -> None:
    entries = json.loads(path.read_text()) if path.exists() else []
    entries.append(
        {
            "name": config.name,
            "url": config.url,
            "command": list(config.command) if config.command else None,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2))


def register_mcp_tools(
    registry: ToolRegistry,
    manager: McpManager,
    audit: AuditLog,
    path: Path = SELF_ADDED_FILE,
) -> None:
    """Expose add_mcp_server backed by the manager."""

    async def add_mcp_server(
        name: str,
        rationale: str,
        url: str | None = None,
        command: Any = None,
    ) -> str:
        if (url is None) == (command is None):
            return "error: give exactly one of url or command"
        if command is not None and not (
            isinstance(command, list) and all(isinstance(c, str) for c in command)
        ):
            return "error: command must be a list of strings"
        config = ServerConfig(
            name=name, url=url, command=tuple(command) if command else None
        )
        try:
            count = await manager.connect(config)
        except Exception as exc:
            return f"error: could not connect to '{name}': {exc}"
        _persist(config, path)
        audit.record(
            "mcp_server_added", name=name, url=url, command=command, why=rationale
        )
        return f"mcp server '{name}' connected with {count} tools"

    registry.register(Tool(_ADD_SPEC, add_mcp_server))
