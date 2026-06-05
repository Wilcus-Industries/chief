"""The ``mcp-gmail`` server: tool catalog + the SDK ``mcp_servers`` entry.

chief wraps the third-party ``MindMadeLab/mcp-google-gmail`` (pinned ``0.1.4``) in its
own container (``docker/mcp-gmail``), streamable HTTP on port 8004, MCP at ``/mcp``,
with a server-side guard that appends a transparent "sent by an assistant" signature to
every outbound message. This module names the package's tools and partitions them for
the gate.

Wiring rules (DESIGN: reads ALLOW, writes ASK): list/get/search reads are pre-approved;
sending, replying, drafting, and label/trash mutations stay out of ``allowed_tools`` so
each reaches the owner's approval card. Permanent deletes are deferred (hard-blocked) —
trashing is reversible (``untrash``) so it stays a gated write, mirroring how the
calendar keeps ``delete-event`` deferred.
"""

from ..google import GoogleService, qualified

#: The SDK names an MCP tool ``mcp__<server>__<tool>``; this is the ``<server>`` half.
SERVER_NAME = "gmail"

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
    """The :class:`GoogleService` for the gmail container at ``url``."""
    return GoogleService(
        name="gmail",
        server_name=SERVER_NAME,
        url=url,
        read_tools=READ_TOOLS,
        write_tools=WRITE_TOOLS,
        deferred_tools=DEFERRED_TOOLS,
    )
