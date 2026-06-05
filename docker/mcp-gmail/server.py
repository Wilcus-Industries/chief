"""Gmail MCP server — third-party ``mcp-google-gmail`` wrapped for chief.

We don't reimplement Gmail; we import the pinned ``MindMadeLab/mcp-google-gmail``
FastMCP instance and wrap it (mirrors ``docker/mcp-sheets/server.py``):

- a server-side guard (``gmail_signature``) appends a transparent "sent by an assistant"
  signature to the body of every outbound message (send / reply / draft create+update),
  by monkeypatching the tool manager's ``call_tool`` — so the recipient always knows an
  assistant sent it, independent of what the model wrote,
- transport is set to ``streamable-http`` (chief reaches it at ``/mcp``) and a
  ``/health`` route is added for the compose healthcheck,
- host/port are pinned for the container.

Auth is the package's own: it reads ``GMAIL_CREDENTIALS_PATH`` (the OAuth client) and
``GMAIL_TOKEN_PATH`` (the minted token) **at import time**. The shared token file is
mounted read-only (the sheets container is its sole writer); ``mcp-google-gmail`` writes
the token back on refresh, so we point ``GMAIL_TOKEN_PATH`` at a *writable per-container
copy* seeded here from the read-only mount before importing the package — refresh writes
hit the throwaway copy, the shared file keeps one writer. Standalone image — no chief
import.
"""

import os
import shutil
from typing import Any

PORT = int(os.environ.get("PORT", "8004"))

#: The read-only shared-token mount → a writable copy the gmail package may rewrite on
#: access-token refresh. Seeded once at startup, before importing the package (which
#: reads ``GMAIL_TOKEN_PATH`` at import). Keeps the shared file single-writer (sheets).
SEED_TOKEN_PATH = os.environ.get("GMAIL_SEED_TOKEN_PATH", "/seed/google_token.json")
TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", "/tmp/gmail_token.json")

if os.path.exists(SEED_TOKEN_PATH):
    shutil.copyfile(SEED_TOKEN_PATH, TOKEN_PATH)
# The package reads this at import; ensure it points at the writable copy.
os.environ["GMAIL_TOKEN_PATH"] = TOKEN_PATH

#: The transparent signature appended to every outbound body. ``{owner}`` (if present) is
#: filled from ``OWNER_NAME`` so the line reads naturally; absent → "the owner".
_OWNER = os.environ.get("OWNER_NAME") or "the owner"
# `or` (not a get default) so an empty compose env falls back to the real multi-line
# default — a literal "" would otherwise disable the signature entirely.
_RAW_SIGNATURE = (
    os.environ.get("GMAIL_SIGNATURE")
    or "—\nSent by {owner}'s assistant on their behalf."
)
try:
    SIGNATURE = _RAW_SIGNATURE.format(owner=_OWNER)
except (KeyError, IndexError, ValueError):
    # A signature with stray braces shouldn't crash the server — use it verbatim.
    SIGNATURE = _RAW_SIGNATURE

from mcp_google_gmail.server import mcp  # noqa: E402  (env must be set before import)

from gmail_signature import inject_signature  # noqa: E402

_original_call_tool = mcp._tool_manager.call_tool


async def _signed_call_tool(
    name: str, arguments: dict[str, Any], **kwargs: Any
) -> Any:
    arguments = inject_signature(name, arguments, SIGNATURE)
    return await _original_call_tool(name, arguments, **kwargs)


mcp._tool_manager.call_tool = _signed_call_tool  # type: ignore[method-assign]

mcp.settings.host = "0.0.0.0"
mcp.settings.port = PORT


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Any) -> Any:
    """Liveness probe for the compose healthcheck."""
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
