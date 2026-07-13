"""Owner-only Messages history tools (#155): read/search, strictly read-only.

An in-process MCP server (``chief_apple_messages``) reading the Messages
conversation store (``~/Library/Messages/chat.db``) through the sqlite3 CLI —
``-readonly`` at the runner seam, and no write tool exists here by design: sending
and the conversation channel belong to the iMessage adapter PRD, which builds on this
read layer. Access requires the Full Disk Access TCC grant (the doctor probes it).

Message text arrives from **other people**, so both tools are seeded into
``screening_tools`` (:mod:`chief.config`) — the same untrusted-content posture as
Gmail reads. Known caveat: recent macOS versions store some message text only in the
``attributedBody`` blob; rows with a NULL ``text`` are skipped in v1 (the live suite
is the canary for how much that misses).
"""

import json
from dataclasses import dataclass
from typing import Any

from ..inprocess import (
    InProcessServerConfig,
    InProcessTool,
    create_sdk_mcp_server,
    tool,
)
from .runner import ScriptRunner, script_error_result, text_result

SERVER_NAME = "chief_apple_messages"
SEARCH_TOOL = f"mcp__{SERVER_NAME}__search_messages"
RECENT_TOOL = f"mcp__{SERVER_NAME}__recent_messages"

#: Both read tools return other people's text — screened for prompt injection like
#: Gmail reads (seeded into ``screening_tools`` by :mod:`chief.config`).
READ_TOOL_NAMES: tuple[str, ...] = (SEARCH_TOOL, RECENT_TOOL)

#: Bounds on one query's row count (a LIMIT is always emitted).
DEFAULT_LIMIT = 20
MAX_LIMIT = 200

#: Messages stores dates as Apple epoch (2001-01-01) nanoseconds; render local time.
_SELECT = (
    "SELECT datetime(message.date/1000000000 + strftime('%s','2001-01-01'), "
    "'unixepoch', 'localtime') AS timestamp, "
    "COALESCE(handle.id, 'me') AS sender, message.is_from_me AS is_from_me, "
    "message.text AS text FROM message "
    "LEFT JOIN handle ON message.handle_id = handle.ROWID "
    "WHERE message.text IS NOT NULL AND message.text != ''"
)


def _like_escape(term: str) -> str:
    r"""Escape a term for a single-quoted ``LIKE '%…%' ESCAPE '\'`` pattern.

    Doubles single quotes (SQL string escaping) and backslash-escapes the LIKE
    wildcards, so owner text can't alter the query shape. The sqlite3 CLI takes one
    query string (no parameter binding at the subprocess seam), and ``-readonly``
    already caps the blast radius at reads.
    """
    escaped = term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return escaped.replace("'", "''")


def _clamp_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def build_search_query(term: str, limit: int) -> str:
    """The exact SQL for a text search, newest first."""
    return (
        f"{_SELECT} AND message.text LIKE '%{_like_escape(term)}%' ESCAPE '\\' "
        f"ORDER BY message.date DESC LIMIT {int(limit)};"
    )


def build_recent_query(contact: str, limit: int) -> str:
    """The exact SQL for recent messages, optionally filtered by handle."""
    handle_filter = (
        f" AND handle.id LIKE '%{_like_escape(contact)}%' ESCAPE '\\'"
        if contact
        else ""
    )
    return (
        f"{_SELECT}{handle_filter} "
        f"ORDER BY message.date DESC LIMIT {int(limit)};"
    )


def _format_rows(raw: str) -> str:
    """Render sqlite3 ``-json`` output as readable message lines.

    The CLI prints *nothing* for an empty result set, so blank stdout is "no
    matches", not an error.
    """
    rows = json.loads(raw) if raw.strip() else []
    if not rows:
        return "No messages matched."
    lines = []
    for row in rows:
        direction = (
            f"me → {row.get('sender', '?')}"
            if row.get("is_from_me")
            else f"{row.get('sender', '?')} → me"
        )
        lines.append(f"[{row.get('timestamp', '?')}] {direction}: "
                     f"{row.get('text', '')}")
    return "\n".join(lines)


_SEARCH_DESCRIPTION = (
    "Search the owner's Messages (iMessage/SMS) history for a text substring. "
    "Read-only. Returns matching messages newest-first with sender and time. "
    "For a phone number or email, look it up with lookup_contact first."
)
_RECENT_DESCRIPTION = (
    "Read the owner's most recent Messages (iMessage/SMS), newest-first. Read-only. "
    "Optionally filter to one conversation partner by their handle (phone number or "
    "email — resolve a name via lookup_contact first)."
)

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Substring to search for."},
        "limit": {"type": "integer", "description": "Max rows (default 20)."},
    },
    "required": ["query"],
}
_RECENT_SCHEMA = {
    "type": "object",
    "properties": {
        "contact": {
            "type": "string",
            "description": "Optional handle filter (phone/email substring).",
        },
        "limit": {"type": "integer", "description": "Max rows (default 20)."},
    },
    "required": [],
}


@dataclass(frozen=True)
class MessagesService:
    """Builds the owner session's read-only Messages history server."""

    runner: ScriptRunner
    db_path: str
    server_name: str = SERVER_NAME
    capability: str = "messages"

    def _build_search(self) -> InProcessTool:
        runner, db_path = self.runner, self.db_path

        @tool("search_messages", _SEARCH_DESCRIPTION, _SEARCH_SCHEMA)
        async def search_messages(args: dict[str, Any]) -> dict[str, Any]:
            query = str(args.get("query", "")).strip()
            if not query:
                return text_result("No search text provided.", is_error=True)
            limit = _clamp_limit(args.get("limit", DEFAULT_LIMIT))
            result = await runner.run_sqlite(
                db_path, build_search_query(query, limit)
            )
            if not result.ok:
                return script_error_result("search Messages history", result)
            try:
                return text_result(_format_rows(result.stdout))
            except ValueError:
                return text_result(
                    f"Messages returned unparseable output: {result.stdout[:200]}",
                    is_error=True,
                )

        return search_messages

    def _build_recent(self) -> InProcessTool:
        runner, db_path = self.runner, self.db_path

        @tool("recent_messages", _RECENT_DESCRIPTION, _RECENT_SCHEMA)
        async def recent_messages(args: dict[str, Any]) -> dict[str, Any]:
            contact = str(args.get("contact", "")).strip()
            limit = _clamp_limit(args.get("limit", DEFAULT_LIMIT))
            result = await runner.run_sqlite(
                db_path, build_recent_query(contact, limit)
            )
            if not result.ok:
                return script_error_result("read Messages history", result)
            try:
                return text_result(_format_rows(result.stdout))
            except ValueError:
                return text_result(
                    f"Messages returned unparseable output: {result.stdout[:200]}",
                    is_error=True,
                )

        return recent_messages

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the Messages read tools."""
        return create_sdk_mcp_server(
            self.server_name, tools=[self._build_search(), self._build_recent()]
        )
