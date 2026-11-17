"""Where one MCP server's OAuth tokens and client registration live.

``secrets/mcp-oauth/<server>.json`` — mode 0600 in a 0700 directory, written
tmp-then-replace so a crash never leaves half a file. The record is bound to the
server url *and* the redirect URI the client was registered with: change either
and the stored registration and tokens read as absent, so the next login
registers afresh instead of presenting a registration the server will refuse.
Beside the login it keeps what the SDK's discovery found (chiefly where the
token endpoint is), which the SDK itself never restores.
"""

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.client.auth.oauth2 import OAuthContext
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp.shared.auth_utils import calculate_token_expiry

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")

#: The ``OAuthContext`` attributes discovery fills in, with the model each is.
_DISCOVERED: dict[str, type[OAuthMetadata] | type[ProtectedResourceMetadata]] = {
    "oauth_metadata": OAuthMetadata,
    "protected_resource_metadata": ProtectedResourceMetadata,
}


class FileTokenStorage:
    """The SDK's ``TokenStorage`` for one server, persisted to one json file.

    ``writable`` is asked before every write. Logout deletes the file while the
    connection it ends can still refresh on its way out; answering False from
    then on keeps that refresh from writing the login back."""

    def __init__(
        self,
        directory: Path,
        server: str,
        url: str,
        redirect_uri: str,
        writable: Callable[[], bool] = lambda: True,
    ) -> None:
        if not _SAFE_NAME.fullmatch(server):
            raise ValueError(f"unsafe mcp server name {server!r} for a token file")
        self._path = directory / f"{server}.json"
        self._binding = {"url": url, "redirect_uri": redirect_uri}
        self._writable = writable
        self._context: OAuthContext | None = None

    def attach(self, context: OAuthContext) -> None:
        """Let loaded tokens carry what the SDK would otherwise lose onto its context.

        The SDK restores stored tokens without an expiry (it then treats them as
        valid forever, and a 401 starts a full login rather than a refresh) and
        without what discovery found, so the first refresh after a restart posts
        to a guessed ``<mcp origin>/token`` — the refresh token and client id sent
        to a host the server never named. ``get_tokens`` puts both back, and
        ``set_tokens`` reads this context for the discovery to store."""
        self._context = context

    async def get_tokens(self) -> OAuthToken | None:
        record = self._load()
        if self._context is not None:
            self._restore(self._context, record)
        tokens = record.get("tokens")
        return OAuthToken.model_validate(tokens) if tokens else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        record = self._load()
        previous = (record.get("tokens") or {}).get("refresh_token")
        if tokens.refresh_token is None and previous:
            # A refresh response may omit the refresh token (RFC 6749 §6); the
            # old one stays good, and the SDK holds this same object in memory.
            tokens.refresh_token = previous
        self._save(
            tokens=tokens.model_dump(mode="json", exclude_none=True),
            expires_at=calculate_token_expiry(tokens.expires_in),
            **self._discovered(),
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        info = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._save(client_info=client_info.model_dump(mode="json", exclude_none=True))

    def revocation_endpoint(self) -> str | None:
        """The authorization server's revocation endpoint, when it advertised one."""
        metadata = self._load().get("oauth_metadata") or {}
        return metadata.get("revocation_endpoint") or None

    def clear(self) -> None:
        """Forget this server entirely (logout)."""
        self._path.unlink(missing_ok=True)

    def _restore(self, context: OAuthContext, record: dict[str, Any]) -> None:
        context.token_expiry_time = record.get("expires_at")
        try:
            for name, model in _DISCOVERED.items():
                # What this session discovered itself is fresher than the file.
                if getattr(context, name) is None and record.get(name):
                    setattr(context, name, model.model_validate(record[name]))
        except ValueError:  # pydantic's ValidationError, e.g. after an SDK upgrade
            logger.warning("mcp oauth file %s: unreadable discovery data", self._path)
        stored_server = record.get("auth_server_url")
        context.auth_server_url = context.auth_server_url or stored_server

    def _discovered(self) -> dict[str, Any]:
        """What discovery has found so far, as fields to store (none while this
        storage is not attached, or before any 401 sent the SDK discovering)."""
        context = self._context
        if context is None:
            return {}
        fields: dict[str, Any] = {
            name: found.model_dump(mode="json", exclude_none=True)
            for name in _DISCOVERED
            if (found := getattr(context, name)) is not None
        }
        if context.auth_server_url:
            fields["auth_server_url"] = context.auth_server_url
        return fields

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
        if not self._writable():
            logger.info("dropped a write to %s from a logged-out session", self._path)
            return
        record = {**self._load(), **self._binding, **fields}
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)  # a leftover could carry looser permissions
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT, 0o600), "w") as file:
            json.dump(record, file)
        os.replace(tmp, self._path)
