"""Transparent "sent by an assistant" signature for the Gmail MCP server.

Pure stdlib so a unit test can import it by path outside the vendored image (the
``docker/`` tree is excluded from chief's suite and the ``mcp-google-gmail`` package
isn't in the venv). ``server.py`` imports :func:`inject_signature` and wires the
monkeypatch on the tool manager — the same hook the sheets row-1 guard uses.

The signature is appended to the ``body`` (and ``html_body``, when present) of every
tool that composes a message a human will read — send, reply, and draft create/update —
so the recipient always knows an assistant sent it on the owner's behalf. ``send_draft``
is deliberately NOT signed: the draft it sends already carries the signature from its
create/update. Reads and label/trash ops carry no body and pass through untouched.

The signature *text* is resolved by ``server.py`` (from the ``GMAIL_SIGNATURE`` env) and
passed in — this module never reads the environment, so the predicate stays a pure,
testable function.
"""

from typing import Any

#: Tools whose ``body`` the recipient reads — the only ones we sign. ``gmail_send_draft``
#: is absent on purpose (its draft was already signed at create/update time).
SIGNATURE_TOOLS = frozenset(
    {
        "gmail_send_message",
        "gmail_reply_on_message",
        "gmail_create_draft",
        "gmail_update_draft",
    }
)


def _append_text(body: str, signature: str) -> str:
    """Append the plain-text signature, blank-line separated (no leading blanks)."""
    return f"{body}\n\n{signature}" if body else signature


def _append_html(html: str, signature: str) -> str:
    """Append the signature to an HTML body, newlines rendered as ``<br>``."""
    sig_html = signature.replace("\n", "<br>")
    return f"{html}<br><br>{sig_html}" if html else sig_html


def inject_signature(
    name: str, arguments: dict[str, Any], signature: str
) -> dict[str, Any]:
    """Return ``arguments`` with ``signature`` appended to the body of a signed tool.

    A no-op (returns the input unchanged) for read/label/trash tools, for
    ``gmail_send_draft``, and when ``signature`` is empty. For a signed tool, returns a
    *new* dict (the original is left untouched) with ``body`` — and ``html_body`` when
    the model supplied one — extended. An absent/``None`` ``body`` (an ``update_draft``
    that keeps the existing body) is left alone, so the already-signed draft body stands.
    """
    if name not in SIGNATURE_TOOLS or not signature:
        return arguments
    updated = dict(arguments)
    body = updated.get("body")
    if isinstance(body, str):
        updated["body"] = _append_text(body, signature)
    html = updated.get("html_body")
    if isinstance(html, str):
        updated["html_body"] = _append_html(html, signature)
    return updated
