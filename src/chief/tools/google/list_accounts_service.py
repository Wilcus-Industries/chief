"""Owner-only in-process ``list_accounts`` MCP tool (issue #44).

Exposes the Google account registry to the owner agent as a single, pre-approved
tool — no approval card needed since this is a pure read with zero side-effects.
Guests never see this service: it is wired into owner sessions only (the same
tier-isolation-by-construction pattern as
:class:`~chief.tools.guest.GuestAdminService`).

The service is built once at startup with a snapshot of the discovered accounts, so
the list reflects the token store at boot time (a restart picks up new tokens).
"""

from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    McpSdkServerConfig,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)

from .accounts import GoogleAccount

_SERVER_NAME = "chief_accounts"

_LIST_DESCRIPTION = (
    "List the Google accounts chief has credentials for. Returns each account's label "
    "and email address. Use this when the owner asks which Google accounts are "
    "registered."
)


def _text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": False}


@dataclass(frozen=True)
class ListAccountsService:
    """Builds the owner session's ``list_accounts`` in-process tool.

    ``accounts`` is the snapshot from
    :func:`~chief.tools.google.accounts.discover_accounts` at startup.  It is baked
    into the closure, so the model reads the registry the process was started with
    (no live filesystem reads per call).
    """

    accounts: list[GoogleAccount] = field(default_factory=list)
    server_name: str = _SERVER_NAME

    @property
    def tool_name(self) -> str:
        """SDK-qualified name: ``mcp__chief_accounts__list_accounts``."""
        return f"mcp__{self.server_name}__list_accounts"

    def _build_tool(self) -> SdkMcpTool[Any]:
        accounts = self.accounts

        @tool("list_accounts", _LIST_DESCRIPTION, {})
        async def list_accounts(_args: dict[str, Any]) -> dict[str, Any]:
            if not accounts:
                return _text_result(
                    "No Google accounts registered. Run the auth script to add one."
                )
            lines = [
                f"- {a.label}"
                + (f" ({a.email})" if a.email and a.email != a.label else "")
                for a in accounts
            ]
            header = f"{len(accounts)} Google account(s) registered:"
            return _text_result(header + "\n" + "\n".join(lines))

        return list_accounts

    def server_config(self) -> McpSdkServerConfig:
        """The in-process ``mcp_servers`` entry for this service."""
        return create_sdk_mcp_server(self.server_name, tools=[self._build_tool()])
