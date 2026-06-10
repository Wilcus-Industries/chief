"""The ``mcp-gmail`` servers: tool catalogs + SDK ``mcp_servers`` entries.

Two Gmail servers coexist during the transition (issue #48 → #52):

- ``mcp-gmail`` (port 8004) — third-party ``MindMadeLab/mcp-google-gmail`` (0.1.4),
  still active; cutover happens in issue #52.
- ``mcp-gmail-chief`` (port 8005) — chief's own FastMCP server (issue #48), multi-
  account, mirrors the calendar/drive/sheets pattern.

:func:`service` wires the 3rd-party server (SDK server name ``gmail``).
:func:`chief_service` wires the new chief-owned server (SDK server name
``gmail_chief``), which reads ``X-Account-Label`` for per-request account selection
identical to the calendar server (issue #46/#56).

Wiring rules (DESIGN: reads ALLOW, writes ASK): list/get/search reads are pre-approved;
sending, replying, drafting, and label/trash mutations stay out of ``allowed_tools`` so
each reaches the owner's approval card. Permanent deletes are deferred (hard-blocked) —
trashing is reversible (``untrash``) so it stays a gated write, mirroring how the
calendar keeps ``delete-event`` deferred.
"""

from ..google import GoogleService, qualified

#: The SDK names an MCP tool ``mcp__<server>__<tool>``; this is the ``<server>`` half.
SERVER_NAME = "gmail"

#: Server name for the chief-owned Gmail server (issue #48).  Distinct from
#: ``SERVER_NAME`` so both servers can be registered in the same MCP session during
#: the transition period; the 3rd-party server is removed in issue #52.
CHIEF_SERVER_NAME = "gmail_chief"

#: Read-only Gmail tools — the gate ALLOWs these with no approval card. Names track
#: ``mcp-google-gmail`` 0.1.4 (pinned in docker/mcp-gmail/requirements.txt).
READ_TOOLS: tuple[str, ...] = qualified(
    SERVER_NAME,
    "gmail_list_messages",
    "gmail_get_message",
    "gmail_search_messages",
    "gmail_list_drafts",
    "gmail_list_labels",
)

#: Read-only tools on the chief-owned Gmail server (same logical names, different
#: SDK namespace).  These are the pre-approved reads for the new server.
CHIEF_READ_TOOLS: tuple[str, ...] = qualified(
    CHIEF_SERVER_NAME,
    "gmail_list_messages",
    "gmail_get_message",
    "gmail_search_messages",
    "gmail_list_drafts",
    "gmail_list_labels",
)

#: Effectful Gmail tools — wired for the owner but routed through ASK → approval. Every
#: outbound message also gets the transparent signature appended server-side. Trash is
#: here (not deferred) because it is reversible via ``gmail_untrash_message``.
WRITE_TOOLS: tuple[str, ...] = qualified(
    SERVER_NAME,
    "gmail_send_message",
    "gmail_reply_on_message",
    "gmail_create_draft",
    "gmail_update_draft",
    "gmail_send_draft",
    "gmail_create_label",
    "gmail_modify_message_labels",
    "gmail_trash_message",
    "gmail_untrash_message",
)

#: Hard-blocked via ``disallowed_tools`` — permanent, irreversible deletes. (Trashing a
#: message is a reversible gated write; deleting a draft or a label is not.)
DEFERRED_TOOLS: tuple[str, ...] = qualified(
    SERVER_NAME, "gmail_delete_draft", "gmail_delete_label"
)


def service(url: str) -> GoogleService:
    """The :class:`GoogleService` for the 3rd-party gmail container at ``url``."""
    return GoogleService(
        name="gmail",
        server_name=SERVER_NAME,
        url=url,
        read_tools=READ_TOOLS,
        write_tools=WRITE_TOOLS,
        deferred_tools=DEFERRED_TOOLS,
    )


def chief_service(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> GoogleService:
    """The :class:`GoogleService` for the chief-owned gmail server at ``url``.

    ``headers`` is forwarded to every HTTP call the SDK makes to the MCP server —
    used to inject ``X-Account-Label`` for multi-account credential selection
    (issue #48).  ``None`` → no extra headers (single-account / no binding).

    This server runs alongside the 3rd-party server during the transition; it is
    read-only (no write/deferred tools yet — those come in #51/#52).
    """
    return GoogleService(
        name="gmail_chief",
        server_name=CHIEF_SERVER_NAME,
        url=url,
        read_tools=CHIEF_READ_TOOLS,
        write_tools=(),
        deferred_tools=(),
        headers=headers,
    )
