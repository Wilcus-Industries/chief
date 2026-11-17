"""MCP server config: one ``mcp_servers`` entry from config.yaml, parsed."""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from chief.mcpclient.headers import expand_headers

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class ServerConfig:
    """One MCP server: exactly one of url (HTTP) or command (stdio).

    ``env`` and ``cwd`` apply to stdio servers only. Environment is opt-in:
    the child gets the SDK's minimal inherited set (``PATH``, `HOME`, etc.)
    plus exactly the keys in ``env`` — never the daemon's full environment —
    so a package can hand a server a credentials path without a wrapper
    script and an operator can audit the grant by reading ``config.yaml``.

    ``headers`` apply to url servers only and arrive here already expanded
    (``${secret:name}`` resolved), so they stay out of ``repr``. ``auth`` is
    ``"oauth"`` for a server that logs in through the broker (url servers only;
    its name becomes a token file name), ``scopes`` what that login asks for.

    ``timeout`` bounds how long ``connect`` waits for this server to become
    ready — a server that resolves or builds dependencies on first launch can
    outrun the default. A server declaring none gets
    ``DEFAULT_CONNECT_TIMEOUT_SECONDS``.
    """

    name: str
    url: str | None = None
    command: tuple[str, ...] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    headers: dict[str, str] | None = field(default=None, repr=False)
    auth: Literal["oauth"] | None = None
    scopes: tuple[str, ...] | None = None


def server_config_from_entry(name: str, entry: dict[str, Any]) -> ServerConfig | None:
    """Build a ``ServerConfig`` from a ``config.yaml`` entry; ``None`` (logged)
    on a malformed ``timeout`` or ``headers`` so a typo or a missing secret
    confines its damage to this one server, not the whole daemon boot."""
    try:
        timeout = _timeout(entry)
        headers = _headers(entry)
        auth = _auth(name, entry)
        scopes = _scopes(entry)
    except ValueError as exc:
        logger.warning("mcp server %r skipped: %s", name, exc)
        return None
    return ServerConfig(
        name=name, url=entry.get("url"), cwd=entry.get("cwd"), timeout=timeout,
        command=tuple(entry["command"]) if entry.get("command") else None,
        env=dict(entry["env"]) if entry.get("env") else None,
        headers=headers, auth=auth, scopes=scopes,
    )


def _timeout(entry: dict[str, Any]) -> float:
    raw = entry.get("timeout")
    if raw is None:
        return DEFAULT_CONNECT_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = -1.0
    if timeout <= 0:
        raise ValueError(f"invalid timeout {raw!r}")
    return timeout


def _headers(entry: dict[str, Any]) -> dict[str, str] | None:
    raw = entry.get("headers")
    if raw is None:
        return None
    if entry.get("command"):
        raise ValueError("headers apply to url servers only")
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and key.strip() and isinstance(value, str)
        for key, value in raw.items()
    ):
        raise ValueError("headers must map non-empty names to string values")
    return expand_headers(raw) if raw else None


def _auth(name: str, entry: dict[str, Any]) -> Literal["oauth"] | None:
    auth = entry.get("auth")
    if auth is None:
        return None
    if auth != "oauth":
        raise ValueError(f"auth must be 'oauth', got {auth!r}")
    if entry.get("command"):
        raise ValueError("auth applies to url servers only")
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError("an oauth server's name may only use letters, digits, - and _")
    return "oauth"


def _scopes(entry: dict[str, Any]) -> tuple[str, ...] | None:
    scopes = entry.get("scopes")
    if scopes is None:
        return None
    if not isinstance(scopes, list) or not all(
        isinstance(scope, str) and scope.strip() for scope in scopes
    ):
        raise ValueError("scopes must be a list of non-empty strings")
    return tuple(scopes)
