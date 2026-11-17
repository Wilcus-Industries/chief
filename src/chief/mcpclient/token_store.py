"""Where one MCP server's OAuth tokens and client registration live.

``secrets/mcp-oauth/<server>.json`` — mode 0600 in a 0700 directory, written
tmp-then-replace so a crash never leaves half a file. The record is bound to the
server url *and* the redirect URI the client was registered with: change either
and the stored registration and tokens read as absent, so the next login
registers afresh instead of presenting a registration the server will refuse.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from mcp.client.auth.oauth2 import OAuthContext
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.shared.auth_utils import calculate_token_expiry

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")


class FileTokenStorage:
    """The SDK's ``TokenStorage`` for one server, persisted to one json file."""

    def __init__(
        self, directory: Path, server: str, url: str, redirect_uri: str
    ) -> None:
        if not _SAFE_NAME.fullmatch(server):
            raise ValueError(f"unsafe mcp server name {server!r} for a token file")
        self._path = directory / f"{server}.json"
        self._binding = {"url": url, "redirect_uri": redirect_uri}
        self._context: OAuthContext | None = None

    def attach(self, context: OAuthContext) -> None:
        """Let loaded tokens carry their expiry onto the SDK's context.

        The SDK restores stored tokens without one and then treats them as valid
        forever, and a 401 starts a full login rather than a refresh — so a
        restart would trade a refreshable token for a new login the first time
        it ran out. Setting the expiry (and the discovered revocation endpoint
        on the way back) is all this needs from the context."""
        self._context = context

    async def get_tokens(self) -> OAuthToken | None:
        record = self._load()
        if self._context is not None:
            self._context.token_expiry_time = record.get("expires_at")
        tokens = record.get("tokens")
        return OAuthToken.model_validate(tokens) if tokens else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        record = self._load()
        previous = (record.get("tokens") or {}).get("refresh_token")
        if tokens.refresh_token is None and previous:
            # A refresh response may omit the refresh token (RFC 6749 §6); the
            # old one stays good, and the SDK holds this same object in memory.
            tokens.refresh_token = previous
        metadata = self._context.oauth_metadata if self._context else None
        found = metadata.revocation_endpoint if metadata else None
        self._save(
            tokens=tokens.model_dump(mode="json", exclude_none=True),
            expires_at=calculate_token_expiry(tokens.expires_in),
            revocation_endpoint=str(found or record.get("revocation_endpoint") or ""),
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        info = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._save(client_info=client_info.model_dump(mode="json", exclude_none=True))

    def revocation_endpoint(self) -> str | None:
        """The authorization server's revocation endpoint, when it advertised one."""
        return self._load().get("revocation_endpoint") or None

    def clear(self) -> None:
        """Forget this server entirely (logout)."""
        self._path.unlink(missing_ok=True)

    def _load(self) -> dict[str, Any]:
        try:
            record = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("mcp oauth file %s is unreadable; ignoring it", self._path)
            return {}
        bound = isinstance(record, dict) and all(
            record.get(key) == value for key, value in self._binding.items()
        )
        return record if bound else {}

    def _save(self, **fields: Any) -> None:
        record = {**self._load(), **self._binding, **fields}
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)  # a leftover could carry looser permissions
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT, 0o600), "w") as file:
            json.dump(record, file)
        os.replace(tmp, self._path)
