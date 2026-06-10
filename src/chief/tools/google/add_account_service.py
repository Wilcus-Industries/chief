"""Owner-only in-process ``add_account`` MCP tool — chat-consent flow (issue #53).

The headless-friendly path to register a new Google account at runtime, with no
redeploy and no callback server on the VPS:

1. The owner asks to add an account. Chief calls ``add_account`` with no ``code`` →
   the tool returns a consent URL (shared Desktop OAuth client, same four-scope
   union) and asks the owner to consent in a browser and paste the loopback
   redirect URL (or bare code) back into chat.
2. The owner pastes it. Chief calls ``add_account`` again with ``code`` → the tool
   exchanges it for a refresh token, resolves the account's email, and stores the
   token (labeled by email) in the secrets dir. It is immediately selectable via
   ``set_account`` — the dynamic registry re-scans on every call (issue #50).

Owner-only and pre-approved (no card): like ``set_account`` it is owner-initiated.
Guests never see this service — it is wired into owner sessions only, mirroring
:class:`~chief.tools.google.set_account_service.SetAccountService`.

The OAuth seams (``exchange`` / ``email_from_credentials``) are injectable so the
flow is unit-tested without a browser or a real Google account.
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

from .auth import (
    SCOPES,
    ClientSecretsMissing,
    EmailFromCredentials,
    ExchangeFn,
    add_account_from_code,
    build_consent_url,
)

logger = logging.getLogger("chief.tools.google.add_account")

_SERVER_NAME = "chief_add_account"

_ADD_DESCRIPTION = (
    "Add a new Google account to chief at runtime via browser consent. "
    "Two-step flow: (1) call with NO arguments to get a consent URL — send it to "
    "the owner and ask them to open it, approve access, then paste the resulting "
    "localhost redirect URL (or the code from it) back into chat. (2) Call again "
    "with that pasted text as `code` to finish: chief exchanges it, stores the "
    "token, and registers the account (labeled by its email). The new account is "
    "then immediately usable via set_account — no restart. Owner-only."
)

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": (
                "The localhost redirect URL or authorization code the owner pasted "
                "after consenting. Omit on the first call to get the consent URL."
            ),
        }
    },
    "required": [],
}


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


@dataclass(frozen=True)
class AddAccountService:
    """Builds the owner session's ``add_account`` in-process tool.

    ``secrets_dir`` is where the minted token file is written (and where the
    registry re-scans), and ``client_secrets`` points at the shared Desktop OAuth
    client JSON. ``exchange`` / ``email_from_credentials`` are injected in tests;
    production uses the live Google endpoints via :func:`add_account_from_code`.
    """

    secrets_dir: Path
    client_secrets: Path
    scopes: tuple[str, ...] = SCOPES
    server_name: str = _SERVER_NAME
    #: Injected OAuth seams (tests only); production defaults hit Google.
    exchange: ExchangeFn | None = field(default=None, repr=False)
    email_from_credentials: EmailFromCredentials | None = field(
        default=None, repr=False
    )

    @property
    def tool_name(self) -> str:
        """SDK-qualified name: ``mcp__chief_add_account__add_account``."""
        return f"mcp__{self.server_name}__add_account"

    def _build_tool(self) -> SdkMcpTool[Any]:
        secrets_dir = self.secrets_dir
        client_secrets = self.client_secrets
        scopes = self.scopes
        exchange = self.exchange
        email_from_credentials = self.email_from_credentials

        @tool("add_account", _ADD_DESCRIPTION, _INPUT_SCHEMA)
        async def add_account(args: dict[str, Any]) -> dict[str, Any]:
            code = (args.get("code") or "").strip()
            if not code:
                # Step 1: hand back the consent URL.
                try:
                    url = build_consent_url(
                        client_secrets=client_secrets, scopes=scopes
                    )
                except ClientSecretsMissing:
                    return _text_result(
                        "No OAuth client configured — the operator must place the "
                        "Desktop-app client JSON in the secrets dir before accounts "
                        "can be added.",
                        is_error=True,
                    )
                return _text_result(
                    "Send the owner this consent link and ask them to approve, then "
                    "paste the resulting localhost URL (or code) back here:\n"
                    f"{url}"
                )

            # Step 2: exchange the pasted code and store the account.
            try:
                _path, email = add_account_from_code(
                    pasted=code,
                    secrets_dir=secrets_dir,
                    client_secrets=client_secrets,
                    scopes=scopes,
                    exchange=exchange,
                    email_from_credentials=email_from_credentials,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("add_account exchange failed: %s", exc)
                return _text_result(
                    f"Could not add the account: {exc}. Ask the owner to retry the "
                    "consent link (the code is one-time and short-lived).",
                    is_error=True,
                )

            logger.info("registered new Google account %r", email)
            return _text_result(
                f"Added Google account {email}. It is now selectable in this thread "
                f"via set_account (use the label {email})."
            )

        return add_account

    def server_config(self) -> McpSdkServerConfig:
        """The in-process ``mcp_servers`` entry for this service."""
        return create_sdk_mcp_server(self.server_name, tools=[self._build_tool()])
