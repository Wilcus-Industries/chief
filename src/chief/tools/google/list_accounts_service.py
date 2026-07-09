"""Owner-only in-process ``list_accounts`` MCP tool (issue #44, #50).

Exposes the Google account registry to the owner agent as a single, pre-approved
tool — no approval card needed since this is a pure read with zero side-effects.
Guests never see this service: it is wired into owner sessions only (the same
tier-isolation-by-construction pattern as
:class:`~chief.tools.guest.GuestAdminService`).

Dynamic discovery (issue #50): when ``secrets_dir`` is supplied the tool re-scans
the token directory on every call so a token dropped after the service was built
appears immediately — no restart needed.  When ``secrets_dir`` is ``None`` (or
absent) the service falls back to the static ``accounts`` snapshot, preserving
full backward compatibility with existing callers.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..inprocess import (
    InProcessServerConfig,
    InProcessTool,
    create_sdk_mcp_server,
    tool,
)
from .accounts import EmailResolver, GoogleAccount, discover_accounts

_SERVER_NAME = "chief_accounts"

_LIST_DESCRIPTION = (
    "List the Google accounts chief has credentials for. Returns each account's label "
    "and email address. Use this when the owner asks which Google accounts are "
    "registered."
)


def _text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": False}


def _format_accounts(accounts: list[GoogleAccount]) -> dict[str, Any]:
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


@dataclass(frozen=True)
class ListAccountsService:
    """Builds the owner session's ``list_accounts`` in-process tool.

    Dynamic mode (issue #50): pass ``secrets_dir`` to re-scan the token directory on
    every ``list_accounts`` call.  A token file dropped after the service was built
    appears immediately without a restart.

    Static mode (backward compat): omit ``secrets_dir`` (or pass ``None``) and supply
    ``accounts`` directly — the tool returns that fixed snapshot.  Existing callers
    that pass ``accounts=discover_accounts(...)`` at boot are unaffected.
    """

    accounts: list[GoogleAccount] = field(default_factory=list)
    server_name: str = _SERVER_NAME
    #: When set, the tool re-scans this directory on every call (dynamic mode).
    secrets_dir: Path | None = None
    #: Resolver passed to discover_accounts in dynamic mode (legacy-token email lookup).
    email_resolver: EmailResolver | None = field(default=None, repr=False)

    @property
    def tool_name(self) -> str:
        """SDK-qualified name: ``mcp__chief_accounts__list_accounts``."""
        return f"mcp__{self.server_name}__list_accounts"

    def _build_tool(self) -> InProcessTool:
        secrets_dir = self.secrets_dir
        email_resolver = self.email_resolver
        # Capture the static snapshot for the no-secrets_dir path.
        static_accounts = self.accounts

        @tool("list_accounts", _LIST_DESCRIPTION, {})
        async def list_accounts(_args: dict[str, Any]) -> dict[str, Any]:
            if secrets_dir is not None:
                # Dynamic: re-scan the token dir on every call.
                accounts = discover_accounts(
                    secrets_dir, email_resolver=email_resolver
                )
            else:
                accounts = static_accounts
            return _format_accounts(accounts)

        return list_accounts

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for this service."""
        return create_sdk_mcp_server(self.server_name, tools=[self._build_tool()])
