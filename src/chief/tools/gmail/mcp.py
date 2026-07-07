"""The chief-owned ``mcp-gmail`` server: tool catalog + SDK ``mcp_servers`` entry.

After the cutover (issue #52) there is a single Gmail server — chief's own FastMCP
server at ``docker/mcp-gmail-chief/server.py`` (multi-account, mirrors the
calendar/drive/sheets pattern). The third-party ``MindMadeLab/mcp-google-gmail``
dependency has been dropped; the ``mcp-gmail`` compose service now runs the chief-owned
image. The SDK server name stays ``gmail_chief`` so the per-thread account-rebuild
branch in ``tasks.py`` (``_build_services_with_account``) keeps matching.

:func:`chief_service` wires the chief-owned server (SDK server name ``gmail_chief``),
which reads ``X-Account-Label`` for per-request account selection identical to the
calendar server (issue #46/#56).

Wiring rules (DESIGN: reads ALLOW, writes ASK): list/get/search reads are pre-approved;
sending, replying, drafting, and label/trash mutations stay out of ``allowed_tools`` —
under the owner's default-allow gate that alone would no longer be enough to card them,
so :data:`WRITE_TOOLS` is also seeded into ``Settings.blacklist_tools`` by default
(``chief.config``), which is what actually routes each to the owner's approval card.
Permanent deletes are deferred (hard-blocked):
``gmail_delete_draft`` / ``gmail_delete_label`` are not even registered as tools on the
server, AND are listed in ``disallowed_tools`` here — belt and suspenders. Trashing is
reversible (``untrash``) so it stays a gated write, mirroring how the calendar keeps
``delete-event`` deferred.
"""

from ..google import GoogleService, qualified

#: Server name for the chief-owned Gmail server (issue #48).  The SDK names an MCP tool
#: ``mcp__<server>__<tool>``; this is the ``<server>`` half.  ``tasks.py``'s per-thread
#: account rebuild matches on the service ``name`` (``gmail_chief``), so keep them in
#: sync.
CHIEF_SERVER_NAME = "gmail_chief"

#: Read-only Gmail tools — the gate ALLOWs these with no approval card.
READ_TOOLS: tuple[str, ...] = qualified(
    CHIEF_SERVER_NAME,
    "gmail_list_messages",
    "gmail_get_message",
    "gmail_search_messages",
    "gmail_list_drafts",
    "gmail_list_labels",
)

#: Effectful Gmail tools — wired for the owner but routed through ASK → approval. Every
#: outbound message (send/reply/draft) also gets the transparent signature appended
#: server-side. Trash is here (not deferred) because it is reversible via
#: ``gmail_untrash_message``.
WRITE_TOOLS: tuple[str, ...] = qualified(
    CHIEF_SERVER_NAME,
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

#: Hard-blocked — permanent, irreversible deletes. The chief-owned server never
#: registers these as tools (the model has no callable to reach), and they are placed
#: in ``disallowed_tools`` here as a second layer. (Trashing a message is a reversible
#: gated write; deleting a draft or a label is not.)
DEFERRED_TOOLS: tuple[str, ...] = qualified(
    CHIEF_SERVER_NAME, "gmail_delete_draft", "gmail_delete_label"
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

    Reads are pre-approved; send/reply/draft and label/trash mutations are writes routed
    through the approval card. Permanent deletes are hard-blocked (deferred).
    """
    return GoogleService(
        name="gmail_chief",
        server_name=CHIEF_SERVER_NAME,
        url=url,
        read_tools=READ_TOOLS,
        write_tools=WRITE_TOOLS,
        deferred_tools=DEFERRED_TOOLS,
        headers=headers,
    )
