"""Owner-only in-process ``set_account`` MCP tool (issue #45, #50).

Exposes per-thread active-account binding to the owner agent as a single,
pre-approved tool — no approval card needed since it is owner-initiated with
no guest-visible side-effects.  Guests never see this service: it is wired
into owner sessions only, mirroring the
:class:`~chief.tools.google.list_accounts_service.ListAccountsService` pattern.

The tool records which registered Google account is active for the current
thread (persisted to sqlite, keyed by ``thread_key``).  Validation rejects
any label/email not present in the registry.

Dynamic discovery (issue #50): when ``secrets_dir`` is supplied the tool
re-scans the token directory on every call so a token dropped after the service
was built is immediately selectable — no restart needed.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    McpSdkServerConfig,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...persistence.tasks import (
    get_active_account,
    get_or_create_task,
    set_active_account,
)
from .accounts import EmailResolver, GoogleAccount, discover_accounts

logger = logging.getLogger("chief.tools.google.set_account")

_SERVER_NAME = "chief_set_account"

_SET_DESCRIPTION = (
    "Set the active Google account for this conversation thread. "
    "Pass the account label or email address of a registered account "
    "(see list_accounts for the available options). "
    "Use this when the owner asks to switch Google accounts or when a task "
    "should run under a specific Google identity."
)

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "account": {
            "type": "string",
            "description": "Label or email of a registered Google account.",
        }
    },
    "required": ["account"],
}


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


def _resolve_account(
    identifier: str, accounts: list[GoogleAccount]
) -> GoogleAccount | None:
    """Find the account whose label or email matches ``identifier``.

    Exact match only: labels are canonical (email when present, slug otherwise),
    so a case-sensitive comparison is consistent with how the registry was built.
    """
    for account in accounts:
        if account.label == identifier:
            return account
        if account.email is not None and account.email == identifier:
            return account
    return None


@dataclass(frozen=True)
class SetAccountService:
    """Builds the owner session's ``set_account`` in-process tool.

    ``session_factory`` is the shared async SQLAlchemy sessionmaker so the tool
    can persist the active-account binding per thread.  ``platform`` scopes the
    DB lookup to the same platform as the engine that wired this service.

    Dynamic mode (issue #50): pass ``secrets_dir`` to re-scan the token directory
    on every ``set_account`` call.  A token dropped after the service was built is
    immediately selectable — no restart needed.

    Static mode (backward compat): omit ``secrets_dir`` and supply ``accounts``
    directly — validation uses that fixed snapshot.
    """

    session_factory: async_sessionmaker[AsyncSession] = field(repr=False)
    accounts: list[GoogleAccount] = field(default_factory=list)
    platform: str = "telegram"
    server_name: str = _SERVER_NAME
    #: When set, the tool re-scans this directory on every call (dynamic mode).
    secrets_dir: Path | None = None
    #: Resolver passed to discover_accounts in dynamic mode (legacy-token email lookup).
    email_resolver: EmailResolver | None = field(default=None, repr=False)

    @property
    def tool_name(self) -> str:
        """SDK-qualified name: ``mcp__chief_set_account__set_account``."""
        return f"mcp__{self.server_name}__set_account"

    def _build_tool(self, thread_key: str) -> SdkMcpTool[Any]:
        """Build the ``set_account`` tool closed over ``thread_key``.

        Each owner session gets its own tool instance so the closure addresses
        the right thread (an in-process MCP handler receives no caller context).
        """
        secrets_dir = self.secrets_dir
        email_resolver = self.email_resolver
        static_accounts = self.accounts
        factory = self.session_factory
        platform = self.platform

        @tool("set_account", _SET_DESCRIPTION, _INPUT_SCHEMA)
        async def set_account(args: dict[str, Any]) -> dict[str, Any]:
            identifier = args.get("account", "")
            if not identifier:
                return _text_result("account is required.", is_error=True)

            # Re-scan on every call when in dynamic mode.
            if secrets_dir is not None:
                accounts = discover_accounts(
                    secrets_dir, email_resolver=email_resolver
                )
            else:
                accounts = static_accounts

            match = _resolve_account(identifier, accounts)
            if match is None:
                labels = ", ".join(a.label for a in accounts) or "(none)"
                return _text_result(
                    f"Account {identifier!r} not found in the registry. "
                    f"Registered accounts: {labels}",
                    is_error=True,
                )

            async with factory() as session:
                task = await get_or_create_task(
                    session,
                    platform=platform,
                    thread_key=thread_key,
                    tier="owner",
                )
                await set_active_account(session, task, match.label)

            logger.info(
                "active account set to %r for thread %s",
                match.label,
                thread_key,
            )
            return _text_result(
                f"Active Google account for this thread set to: {match.label}"
            )

        return set_account

    def server_config(self, *, thread_key: str) -> McpSdkServerConfig:
        """The in-process ``mcp_servers`` entry for this service.

        Unlike :class:`~chief.tools.google.list_accounts_service.ListAccountsService`,
        the server is built per-thread so the closure can address the right row.
        """
        return create_sdk_mcp_server(
            self.server_name, tools=[self._build_tool(thread_key)]
        )

    async def get_active_account_label(self, thread_key: str) -> str | None:
        """Return the currently active account label for ``thread_key``, or ``None``."""
        async with self.session_factory() as session:
            task = await get_or_create_task(
                session,
                platform=self.platform,
                thread_key=thread_key,
                tier="owner",
            )
            return await get_active_account(session, task)
