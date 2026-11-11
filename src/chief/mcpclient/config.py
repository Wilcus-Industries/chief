"""One MCP server's ``config.yaml`` entry, parsed: ``ServerConfig``.

Split out of ``manager`` (file-length cap); ``manager`` re-exports both names.
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ServerConfig:
    """One MCP server: exactly one of url (HTTP) or command (stdio).

    ``env`` and ``cwd`` apply to stdio servers only. Environment is opt-in:
    the child gets the SDK's minimal inherited set (``PATH``, `HOME`, etc.)
    plus exactly the keys in ``env`` — never the daemon's full environment —
    so a package can hand a server a credentials path without a wrapper
    script and an operator can audit the grant by reading ``config.yaml``.

    ``timeout`` bounds how long ``connect`` waits for this server to become
    ready — a server that resolves or builds dependencies on first launch can
    outrun the default. A server declaring none gets
    ``DEFAULT_CONNECT_TIMEOUT_SECONDS``.

    ``session_context`` opts the server in to learning which chief session is
    calling: each tool call carries ``_meta`` ``chief/thread`` and
    ``chief/channel``, so a server can keep per-session state (one browser tab
    per session). Off by default — thread keys are often phone numbers and are
    never sent to a server that did not ask in ``config.yaml``. (An opted-in
    server's tools cannot take an argument named ``context``.)
    """

    name: str
    url: str | None = None
    command: tuple[str, ...] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    session_context: bool = False


def server_config_from_entry(name: str, entry: dict[str, Any]) -> ServerConfig | None:
    """Build a ``ServerConfig`` from a ``config.yaml`` entry; ``None`` on a
    malformed ``timeout`` (non-numeric or <= 0) so a typo confines its
    damage to this one server, not the whole daemon boot."""
    timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
    raw_timeout = entry.get("timeout")
    if raw_timeout is not None:
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            timeout = -1.0
        if timeout <= 0:
            logger.warning("mcp server %r has invalid timeout %r", name, raw_timeout)
            return None
    return ServerConfig(
        name=name, url=entry.get("url"), cwd=entry.get("cwd"), timeout=timeout,
        command=tuple(entry["command"]) if entry.get("command") else None,
        env=dict(entry["env"]) if entry.get("env") else None,
        # Only a real boolean opts in; anything else stays off (fail closed).
        session_context=entry.get("session_context") is True,
    )
