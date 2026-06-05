"""Coverage for the Gmail outbound-signature guard.

``docker/mcp-gmail/gmail_signature.py`` lives in a vendored image (excluded from the
package and the venv), so we load it by path — the same trick ``test_sheets_row1_guard``
uses. The predicate is trust-critical: the personas prompt promises every message chief
sends is transparently marked as assistant-sent, and that marking is appended here
server-side regardless of what the model wrote in the body.
"""

import importlib.util
from pathlib import Path

import pytest

_SIG_PATH = (
    Path(__file__).resolve().parents[1] / "docker" / "mcp-gmail" / "gmail_signature.py"
)
_spec = importlib.util.spec_from_file_location("gmail_signature", _SIG_PATH)
assert _spec and _spec.loader
gmail_signature = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gmail_signature)

SIG = "—\nSent by the assistant."


def test_send_message_appends_to_body() -> None:
    out = gmail_signature.inject_signature(
        "gmail_send_message", {"to": "a@b.c", "subject": "Hi", "body": "Hello."}, SIG
    )
    assert out["body"] == f"Hello.\n\n{SIG}"
    # Other args are preserved untouched.
    assert out["to"] == "a@b.c"
    assert out["subject"] == "Hi"


def test_signs_reply_create_and_update() -> None:
    for name in (
        "gmail_reply_on_message",
        "gmail_create_draft",
        "gmail_update_draft",
    ):
        out = gmail_signature.inject_signature(name, {"body": "Body"}, SIG)
        assert out["body"] == f"Body\n\n{SIG}"


def test_html_body_also_signed_with_br() -> None:
    out = gmail_signature.inject_signature(
        "gmail_send_message",
        {"body": "Hello.", "html_body": "<p>Hello.</p>"},
        SIG,
    )
    assert out["body"] == f"Hello.\n\n{SIG}"
    # Plain-text newlines become <br> in the HTML variant.
    assert out["html_body"] == "<p>Hello.</p><br><br>—<br>Sent by the assistant."


def test_send_draft_is_not_signed() -> None:
    # The draft it sends was already signed at create/update — don't double-sign.
    args = {"draft_id": "d1"}
    assert gmail_signature.inject_signature("gmail_send_draft", args, SIG) is args


def test_reads_and_label_ops_pass_through() -> None:
    for name in (
        "gmail_list_messages",
        "gmail_get_message",
        "gmail_search_messages",
        "gmail_trash_message",
        "gmail_modify_message_labels",
    ):
        args = {"message_id": "m1"}
        assert gmail_signature.inject_signature(name, args, SIG) is args


def test_update_draft_without_body_keeps_existing() -> None:
    # body=None on update means "keep the existing (already-signed) body" — don't touch.
    args = {"draft_id": "d1", "subject": "New subject"}
    out = gmail_signature.inject_signature("gmail_update_draft", args, SIG)
    assert "body" not in out


def test_empty_signature_is_a_noop() -> None:
    args = {"body": "Hello."}
    assert gmail_signature.inject_signature("gmail_send_message", args, "") is args


@pytest.mark.parametrize("empty_body", ["", None])
def test_empty_body_send(empty_body: str | None) -> None:
    args: dict[str, object] = {"to": "a@b.c", "subject": "Hi"}
    if empty_body is not None:
        args["body"] = empty_body
    out = gmail_signature.inject_signature("gmail_send_message", args, SIG)
    if empty_body == "":
        # An empty body still gets the signature (no leading blank lines).
        assert out["body"] == SIG
    else:
        assert "body" not in out
