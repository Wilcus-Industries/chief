"""Server-level tests for the chief-owned Gmail MCP server (issue #48).

Covers:
1. Per-account credential selection on a read tool (X-Account-Label → right service).
2. MIME body walk: plain-text and multipart HTML message parsing.
3. Server module structure (health, signature scaffold, tool names).

The server lives at docker/mcp-gmail-chief/server.py.  Google API deps are mocked at the
sys.modules level (same approach as test_calendar_server_injection.py).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SERVER_PATH = (
    Path(__file__).parent.parent / "docker" / "mcp-gmail-chief" / "server.py"
)
_SERVER_DIR = str(_SERVER_PATH.parent)

# ---------------------------------------------------------------------------
# Google / googleapiclient stubs
# ---------------------------------------------------------------------------


def _make_google_stubs() -> dict[str, Any]:
    stubs: dict[str, Any] = {}

    mock_creds_cls = MagicMock()
    mock_creds_instance = MagicMock()
    mock_creds_instance.expired = False
    mock_creds_instance.refresh_token = "rt_fake"
    mock_creds_cls.from_authorized_user_info.return_value = mock_creds_instance
    mock_creds_cls.from_authorized_user_file.return_value = mock_creds_instance

    google_pkg = types.ModuleType("google")
    google_oauth2 = types.ModuleType("google.oauth2")
    google_oauth2_creds = types.ModuleType("google.oauth2.credentials")
    google_oauth2_creds.Credentials = mock_creds_cls  # type: ignore[attr-defined]

    google_auth = types.ModuleType("google.auth")
    google_auth_transport = types.ModuleType("google.auth.transport")
    google_auth_transport_requests = types.ModuleType("google.auth.transport.requests")
    google_auth_transport_requests.Request = MagicMock()  # type: ignore[attr-defined]

    googleapiclient_pkg = types.ModuleType("googleapiclient")
    googleapiclient_discovery = types.ModuleType("googleapiclient.discovery")
    googleapiclient_discovery.build = MagicMock(  # type: ignore[attr-defined]
        return_value=MagicMock()
    )

    stubs["google"] = google_pkg
    stubs["google.oauth2"] = google_oauth2
    stubs["google.oauth2.credentials"] = google_oauth2_creds
    stubs["google.auth"] = google_auth
    stubs["google.auth.transport"] = google_auth_transport
    stubs["google.auth.transport.requests"] = google_auth_transport_requests
    stubs["googleapiclient"] = googleapiclient_pkg
    stubs["googleapiclient.discovery"] = googleapiclient_discovery
    return stubs


def _load_gmail_chief_server(tmp_token_dir: Path) -> types.ModuleType:
    """Import docker/mcp-gmail-chief/server.py with mocked Google deps."""
    stubs = _make_google_stubs()
    for name, stub in stubs.items():
        sys.modules.setdefault(name, stub)

    if _SERVER_DIR not in sys.path:
        sys.path.insert(0, _SERVER_DIR)

    # A sibling test (test_dynamic_account_discovery) leaves a MagicMock stub for
    # gmail_signature in sys.modules; that breaks the real signature append on
    # send/reply. Force-load the genuine module from disk so the server's
    # ``from gmail_signature import inject_signature`` binds the real predicate.
    real_sig_path = _SERVER_PATH.parent / "gmail_signature.py"
    sig_spec = importlib.util.spec_from_file_location(
        "gmail_signature", real_sig_path
    )
    assert sig_spec is not None and sig_spec.loader is not None
    sig_mod = importlib.util.module_from_spec(sig_spec)
    sig_spec.loader.exec_module(sig_mod)
    sys.modules["gmail_signature"] = sig_mod

    import os

    os.environ["TOKEN_DIR"] = str(tmp_token_dir)
    os.environ["GOOGLE_TOKEN_PATH"] = str(tmp_token_dir / "google_token.json")

    mod_name = f"gmail_chief_server_{id(tmp_token_dir)}"
    spec = importlib.util.spec_from_file_location(mod_name, _SERVER_PATH)
    assert spec is not None and spec.loader is not None
    srv = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = srv
    spec.loader.exec_module(srv)
    return srv


def _write_token(path: Path, label: str) -> None:
    data = {
        "token": f"at_{label}",
        "refresh_token": f"rt_{label}",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "secret",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        "account": label,
    }
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def gmail_server(tmp_path: Path) -> types.ModuleType:
    """Load a fresh Gmail server module with two fake service accounts.

    Patches ``_build_service_registry`` on the loaded module so every per-request
    rescan (issue #60) returns the same deterministic fake registry — the two
    mocked services are always "visible" regardless of TOKEN_DIR contents.
    """
    _write_token(tmp_path / "google_token.json", "main@example.com")
    _write_token(tmp_path / "google_token_work.json", "work@corp.com")

    srv = _load_gmail_chief_server(tmp_path)

    main_svc = MagicMock()
    main_svc.users().messages().list().execute.return_value = {
        "messages": [{"id": "msg_main_1", "threadId": "t1"}]
    }
    work_svc = MagicMock()
    work_svc.users().messages().list().execute.return_value = {
        "messages": [{"id": "msg_work_1", "threadId": "t2"}]
    }
    fake_registry = {
        "main@example.com": main_svc,
        "work@corp.com": work_svc,
    }

    # Patch _build_service_registry so the dynamic rescan always returns the
    # fake registry instead of scanning the real filesystem.
    srv._build_service_registry = (  # type: ignore[attr-defined]
        lambda: (fake_registry, main_svc)
    )
    return srv


@pytest.fixture()
def mcp_client(gmail_server: types.ModuleType) -> Any:
    """Return a (TestClient, session_id) pair on an initialized MCP session."""
    starlette_app = gmail_server.mcp.streamable_http_app()
    client = TestClient(starlette_app, raise_server_exceptions=True)
    client.__enter__()

    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.0.1"},
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 200, f"MCP initialize failed: {r.text}"
    session_id = r.headers.get("mcp-session-id")
    assert session_id, "No Mcp-Session-Id in initialize response"
    return client, session_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_result_text(sse_body: str) -> str:
    for line in sse_body.splitlines():
        if line.startswith("data:"):
            return line[len("data:"):].strip()
    return sse_body


def _first_message_id_from_list(sse_body: str) -> str:
    raw = _extract_result_text(sse_body)
    payload = json.loads(raw)
    content_text = payload["result"]["content"][0]["text"]
    items = json.loads(content_text)
    return items[0]["id"] if items else ""


def _result_payload(sse_body: str) -> dict[str, Any]:
    """Parse the inner tool-result JSON returned by a tools/call SSE response."""
    raw = _extract_result_text(sse_body)
    payload = json.loads(raw)
    content_text = payload["result"]["content"][0]["text"]
    parsed: dict[str, Any] = json.loads(content_text)
    return parsed


# ---------------------------------------------------------------------------
# Tests: per-account credential selection on a read tool
# ---------------------------------------------------------------------------


class TestGmailAccountLabelInjection:
    """X-Account-Label header on tools/call → correct Gmail service per request."""

    def _list_messages(
        self,
        client: TestClient,
        session_id: str,
        *,
        account_label: str | None = None,
    ) -> str:
        extra: dict[str, str] = {}
        if account_label is not None:
            extra["X-Account-Label"] = account_label
        r = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "gmail_list_messages",
                    "arguments": {"max_results": 5},
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Mcp-Session-Id": session_id,
                **extra,
            },
        )
        assert r.status_code == 200, f"tools/call failed: {r.text}"
        return _first_message_id_from_list(r.text)

    def test_work_label_returns_work_inbox(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        client, session_id = mcp_client
        msg_id = self._list_messages(client, session_id, account_label="work@corp.com")
        assert msg_id == "msg_work_1", (
            f"Expected msg_work_1 for work@corp.com, got {msg_id!r}"
        )

    def test_main_label_returns_main_inbox(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        client, session_id = mcp_client
        msg_id = self._list_messages(
            client, session_id, account_label="main@example.com"
        )
        assert msg_id == "msg_main_1", (
            f"Expected msg_main_1 for main@example.com, got {msg_id!r}"
        )

    def test_sequential_different_labels_same_session(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        """Two sequential calls with different labels each pick the right service."""
        client, session_id = mcp_client

        work = self._list_messages(
            client, session_id, account_label="work@corp.com"
        )
        main = self._list_messages(
            client, session_id, account_label="main@example.com"
        )

        assert work == "msg_work_1", f"Expected msg_work_1, got {work!r}"
        assert main == "msg_main_1", f"Expected msg_main_1, got {main!r}"

    def test_absent_header_falls_back_to_default(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        client, session_id = mcp_client
        msg_id = self._list_messages(client, session_id, account_label=None)
        assert msg_id == "msg_main_1", (
            f"Expected default (msg_main_1) when no header, got {msg_id!r}"
        )

    def test_unknown_label_falls_back_to_default(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        client, session_id = mcp_client
        msg_id = self._list_messages(
            client, session_id, account_label="nobody@example.com"
        )
        assert msg_id == "msg_main_1", (
            f"Expected default for unknown label, got {msg_id!r}"
        )


# ---------------------------------------------------------------------------
# Tests: MIME body walk
# ---------------------------------------------------------------------------


class TestMimeBodyWalk:
    """_extract_body correctly parses plain-text and multipart HTML messages."""

    def _get_server_module(self, tmp_path: Path) -> types.ModuleType:
        srv = _load_gmail_chief_server(tmp_path)
        return srv

    def test_plain_text_message(self, tmp_path: Path) -> None:
        """A plain-text message payload returns the decoded text."""
        srv = self._get_server_module(tmp_path)

        import base64

        body_text = "Hello, world!"
        encoded = base64.urlsafe_b64encode(body_text.encode()).decode()
        payload: dict[str, Any] = {
            "mimeType": "text/plain",
            "body": {"data": encoded},
            "parts": [],
        }
        result = srv._extract_body(payload)
        assert result == body_text

    def test_multipart_html_message(self, tmp_path: Path) -> None:
        """A multipart/alternative message returns the text/plain part (preferred)."""
        srv = self._get_server_module(tmp_path)

        import base64

        plain_text = "Plain body"
        html_text = "<p>HTML body</p>"
        plain_enc = base64.urlsafe_b64encode(plain_text.encode()).decode()
        html_enc = base64.urlsafe_b64encode(html_text.encode()).decode()

        payload: dict[str, Any] = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": plain_enc},
                    "parts": [],
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": html_enc},
                    "parts": [],
                },
            ],
        }
        result = srv._extract_body(payload)
        # text/plain is preferred over text/html when both are present
        assert result == plain_text

    def test_html_only_message(self, tmp_path: Path) -> None:
        """A multipart message with only HTML falls back to HTML."""
        srv = self._get_server_module(tmp_path)

        import base64

        html_text = "<p>Only HTML</p>"
        html_enc = base64.urlsafe_b64encode(html_text.encode()).decode()

        payload: dict[str, Any] = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": html_enc},
                    "parts": [],
                }
            ],
        }
        result = srv._extract_body(payload)
        assert result == html_text

    def test_nested_multipart(self, tmp_path: Path) -> None:
        """A nested multipart structure is walked recursively to find text."""
        srv = self._get_server_module(tmp_path)

        import base64

        inner_text = "Nested plain text"
        inner_enc = base64.urlsafe_b64encode(inner_text.encode()).decode()

        payload: dict[str, Any] = {
            "mimeType": "multipart/mixed",
            "body": {},
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "body": {},
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": inner_enc},
                            "parts": [],
                        }
                    ],
                }
            ],
        }
        result = srv._extract_body(payload)
        assert result == inner_text

    def test_empty_payload_returns_empty(self, tmp_path: Path) -> None:
        """A message with no body data and no parts returns an empty string."""
        srv = self._get_server_module(tmp_path)

        payload: dict[str, Any] = {
            "mimeType": "text/plain",
            "body": {},
            "parts": [],
        }
        result = srv._extract_body(payload)
        assert result == ""


# ---------------------------------------------------------------------------
# Tests: health route and server structure
# ---------------------------------------------------------------------------


class TestGmailServerStructure:
    """The server exposes /health and the expected tool names."""

    def test_health_route_returns_ok(self, gmail_server: types.ModuleType) -> None:
        starlette_app = gmail_server.mcp.streamable_http_app()
        with TestClient(starlette_app) as client:
            r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_signature_scaffold_exists(self, gmail_server: types.ModuleType) -> None:
        """The signature scaffold is importable from the server module."""
        assert hasattr(gmail_server, "SIGNATURE"), (
            "server.py must define SIGNATURE (the transparent assistant signature)"
        )

    def test_inject_signature_importable(self, tmp_path: Path) -> None:
        """gmail_signature.inject_signature is importable from the server dir."""
        sig_path = _SERVER_PATH.parent / "gmail_signature.py"
        assert sig_path.exists(), (
            f"gmail_signature.py must exist at {sig_path} for the signature scaffold"
        )


# ---------------------------------------------------------------------------
# Tests: RFC822 build helper (send + reply)
# ---------------------------------------------------------------------------


def _decode_raw(raw: str) -> Any:
    """Decode a base64url ``raw`` field to an ``EmailMessage`` (default policy)."""
    import base64
    from email import message_from_bytes
    from email.policy import default as default_policy

    return message_from_bytes(
        base64.urlsafe_b64decode(raw.encode("ascii")), policy=default_policy
    )


class TestBuildRawMessage:
    """``_build_raw_message`` produces a valid base64url RFC822 message."""

    def test_send_message_round_trips(self, tmp_path: Path) -> None:
        srv = _load_gmail_chief_server(tmp_path)
        raw = srv._build_raw_message(
            to="alice@example.com",
            subject="Hello",
            body="Hi there.",
        )
        msg = _decode_raw(raw)
        assert msg["To"] == "alice@example.com"
        assert msg["Subject"] == "Hello"
        assert "Hi there." in msg.get_content()

    def test_threading_headers_present_on_reply(self, tmp_path: Path) -> None:
        srv = _load_gmail_chief_server(tmp_path)
        raw = srv._build_raw_message(
            to="bob@example.com",
            subject="Re: Hello",
            body="Replying.",
            in_reply_to="<orig@mail.gmail.com>",
            references="<orig@mail.gmail.com>",
        )
        msg = _decode_raw(raw)
        assert msg["In-Reply-To"] == "<orig@mail.gmail.com>"
        assert msg["References"] == "<orig@mail.gmail.com>"

    def test_raw_is_base64url(self, tmp_path: Path) -> None:
        srv = _load_gmail_chief_server(tmp_path)
        raw = srv._build_raw_message(to="a@b.c", subject="S", body="B")
        # urlsafe base64 never contains '+' or '/'.
        assert "+" not in raw and "/" not in raw


# ---------------------------------------------------------------------------
# Tests: send / reply route through the real server tools (ASGI path)
# ---------------------------------------------------------------------------


@pytest.fixture()
def send_capture_server(tmp_path: Path) -> tuple[types.ModuleType, dict[str, Any]]:
    """Load a Gmail server whose two fake services record ``messages().send`` calls.

    Returns ``(srv, captured)`` where ``captured[label]`` is the kwargs of the most
    recent ``send`` on that account's service.
    """
    _write_token(tmp_path / "google_token.json", "main@example.com")
    _write_token(tmp_path / "google_token_work.json", "work@corp.com")

    srv = _load_gmail_chief_server(tmp_path)

    captured: dict[str, Any] = {}

    def _make_svc(label: str) -> MagicMock:
        svc = MagicMock()

        def _send(*, userId: str, body: dict[str, Any]) -> MagicMock:
            captured[label] = {"userId": userId, "body": body}
            exec_mock = MagicMock()
            exec_mock.execute.return_value = {
                "id": f"sent_{label}",
                "threadId": body.get("threadId", f"thr_{label}"),
            }
            return exec_mock

        svc.users().messages().send.side_effect = _send
        return svc

    main_svc = _make_svc("main@example.com")
    work_svc = _make_svc("work@corp.com")
    fake_registry = {"main@example.com": main_svc, "work@corp.com": work_svc}
    srv._build_service_registry = (  # type: ignore[attr-defined]
        lambda: (fake_registry, main_svc)
    )
    return srv, captured


def _send_capture_client(srv: types.ModuleType) -> tuple[TestClient, str]:
    starlette_app = srv.mcp.streamable_http_app()
    client = TestClient(starlette_app, raise_server_exceptions=True)
    client.__enter__()
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.0.1"},
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 200, f"MCP initialize failed: {r.text}"
    session_id = r.headers.get("mcp-session-id")
    assert session_id, "No Mcp-Session-Id in initialize response"
    return client, session_id


def _call_tool(
    client: TestClient,
    session_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    account_label: str | None = None,
) -> str:
    extra: dict[str, str] = {}
    if account_label is not None:
        extra["X-Account-Label"] = account_label
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Session-Id": session_id,
            **extra,
        },
    )
    assert r.status_code == 200, f"tools/call failed: {r.text}"
    return str(r.text)


class TestGmailSendReply:
    """gmail_send_message / gmail_reply_on_message drive the real server tools."""

    def test_send_uses_active_account_and_signs(
        self, send_capture_server: tuple[types.ModuleType, dict[str, Any]]
    ) -> None:
        srv, captured = send_capture_server
        client, session_id = _send_capture_client(srv)
        body = _result_payload(
            _call_tool(
                client,
                session_id,
                "gmail_send_message",
                {"to": "a@b.c", "subject": "Hi", "body": "Hello."},
                account_label="work@corp.com",
            )
        )
        # Sent on the work service only.
        assert "work@corp.com" in captured
        assert "main@example.com" not in captured
        assert body["id"] == "sent_work@corp.com"
        # The signature was appended server-side, independent of the model body.
        raw = captured["work@corp.com"]["body"]["raw"]
        msg = _decode_raw(raw)
        assert srv.SIGNATURE in msg.get_content()

    def test_switching_account_sends_from_other_account(
        self, send_capture_server: tuple[types.ModuleType, dict[str, Any]]
    ) -> None:
        srv, captured = send_capture_server
        client, session_id = _send_capture_client(srv)
        _call_tool(
            client,
            session_id,
            "gmail_send_message",
            {"to": "a@b.c", "subject": "S", "body": "B"},
            account_label="main@example.com",
        )
        assert set(captured) == {"main@example.com"}

    def test_reply_threads_correctly(
        self, send_capture_server: tuple[types.ModuleType, dict[str, Any]]
    ) -> None:
        srv, captured = send_capture_server
        # The reply tool fetches the original message to read its Message-ID/References.
        work_svc = srv._build_service_registry()[0]["work@corp.com"]
        work_svc.users().messages().get().execute.return_value = {
            "id": "orig1",
            "threadId": "THREAD42",
            "payload": {
                "headers": [
                    {"name": "Message-ID", "value": "<orig@mail.gmail.com>"},
                    {"name": "Subject", "value": "Project"},
                    {"name": "From", "value": "bob@corp.com"},
                ]
            },
        }
        client, session_id = _send_capture_client(srv)
        _call_tool(
            client,
            session_id,
            "gmail_reply_on_message",
            {"message_id": "orig1", "body": "Sounds good."},
            account_label="work@corp.com",
        )
        sent = captured["work@corp.com"]["body"]
        assert sent["threadId"] == "THREAD42"
        msg = _decode_raw(sent["raw"])
        assert msg["In-Reply-To"] == "<orig@mail.gmail.com>"
        assert "<orig@mail.gmail.com>" in msg["References"]


# ---------------------------------------------------------------------------
# Tests: remaining writes — drafts, labels, trash/untrash (issue #52)
# ---------------------------------------------------------------------------


@pytest.fixture()
def write_capture_server(
    tmp_path: Path,
) -> tuple[types.ModuleType, dict[str, list[tuple[str, dict[str, Any]]]]]:
    """Load a Gmail server whose two fake services record every write API call.

    Returns ``(srv, captured)`` where ``captured[label]`` is the ordered list of
    ``(api_path, kwargs)`` tuples recorded on that account's service — e.g.
    ``("drafts.create", {...})`` — so a test can assert the right account got the
    right call with the right arguments.
    """
    _write_token(tmp_path / "google_token.json", "main@example.com")
    _write_token(tmp_path / "google_token_work.json", "work@corp.com")

    srv = _load_gmail_chief_server(tmp_path)

    captured: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    def _make_svc(label: str) -> MagicMock:
        svc = MagicMock()
        events = captured.setdefault(label, [])

        def _record(api_path: str, result: dict[str, Any]) -> Any:
            def _fn(**kwargs: Any) -> MagicMock:
                events.append((api_path, kwargs))
                exec_mock = MagicMock()
                exec_mock.execute.return_value = result
                return exec_mock

            return _fn

        users = svc.users.return_value
        users.drafts.return_value.create.side_effect = _record(
            "drafts.create", {"id": f"draft_{label}", "message": {"id": "m1"}}
        )
        users.drafts.return_value.update.side_effect = _record(
            "drafts.update", {"id": f"draft_{label}", "message": {"id": "m1"}}
        )
        users.drafts.return_value.send.side_effect = _record(
            "drafts.send", {"id": f"sent_{label}", "threadId": f"thr_{label}"}
        )
        users.labels.return_value.create.side_effect = _record(
            "labels.create", {"id": f"Label_{label}", "name": "Newsletters"}
        )
        users.messages.return_value.modify.side_effect = _record(
            "messages.modify", {"id": "msg1", "labelIds": ["INBOX", "Label_1"]}
        )
        users.messages.return_value.trash.side_effect = _record(
            "messages.trash", {"id": "msg1", "labelIds": ["TRASH"]}
        )
        users.messages.return_value.untrash.side_effect = _record(
            "messages.untrash", {"id": "msg1", "labelIds": ["INBOX"]}
        )
        return svc

    main_svc = _make_svc("main@example.com")
    work_svc = _make_svc("work@corp.com")
    fake_registry = {"main@example.com": main_svc, "work@corp.com": work_svc}
    srv._build_service_registry = (  # type: ignore[attr-defined]
        lambda: (fake_registry, main_svc)
    )
    return srv, captured


class TestGmailDrafts:
    """Draft create/update/send drive the real server tools over the ASGI path."""

    def test_create_draft_uses_active_account_and_signs(
        self,
        write_capture_server: tuple[
            types.ModuleType, dict[str, list[tuple[str, dict[str, Any]]]]
        ],
    ) -> None:
        srv, captured = write_capture_server
        client, session_id = _send_capture_client(srv)
        result = _result_payload(
            _call_tool(
                client,
                session_id,
                "gmail_create_draft",
                {"to": "a@b.c", "subject": "Hi", "body": "Hello."},
                account_label="work@corp.com",
            )
        )
        assert result["id"] == "draft_work@corp.com"
        # Created on the work account only.
        work_calls = [p for p, _ in captured["work@corp.com"]]
        assert "drafts.create" in work_calls
        assert all(
            p != "drafts.create" for p, _ in captured["main@example.com"]
        )
        # The signature is baked into the draft body server-side.
        _, kwargs = next(
            c for c in captured["work@corp.com"] if c[0] == "drafts.create"
        )
        raw = kwargs["body"]["message"]["raw"]
        msg = _decode_raw(raw)
        assert srv.SIGNATURE in msg.get_content()

    def test_update_draft_replaces_message(
        self,
        write_capture_server: tuple[
            types.ModuleType, dict[str, list[tuple[str, dict[str, Any]]]]
        ],
    ) -> None:
        srv, captured = write_capture_server
        client, session_id = _send_capture_client(srv)
        _call_tool(
            client,
            session_id,
            "gmail_update_draft",
            {
                "draft_id": "draft_work@corp.com",
                "to": "a@b.c",
                "subject": "New",
                "body": "Updated.",
            },
            account_label="work@corp.com",
        )
        _, kwargs = next(
            c for c in captured["work@corp.com"] if c[0] == "drafts.update"
        )
        assert kwargs["id"] == "draft_work@corp.com"
        msg = _decode_raw(kwargs["body"]["message"]["raw"])
        assert srv.SIGNATURE in msg.get_content()

    def test_send_draft_does_not_resign(
        self,
        write_capture_server: tuple[
            types.ModuleType, dict[str, list[tuple[str, dict[str, Any]]]]
        ],
    ) -> None:
        srv, captured = write_capture_server
        client, session_id = _send_capture_client(srv)
        result = _result_payload(
            _call_tool(
                client,
                session_id,
                "gmail_send_draft",
                {"draft_id": "draft_work@corp.com"},
                account_label="work@corp.com",
            )
        )
        assert result["id"] == "sent_work@corp.com"
        _, kwargs = next(
            c for c in captured["work@corp.com"] if c[0] == "drafts.send"
        )
        assert kwargs["body"]["id"] == "draft_work@corp.com"


class TestGmailLabelsAndTrash:
    """create_label / modify_message_labels / trash / untrash on the active account."""

    def test_create_label_on_active_account(
        self,
        write_capture_server: tuple[
            types.ModuleType, dict[str, list[tuple[str, dict[str, Any]]]]
        ],
    ) -> None:
        srv, captured = write_capture_server
        client, session_id = _send_capture_client(srv)
        result = _result_payload(
            _call_tool(
                client,
                session_id,
                "gmail_create_label",
                {"name": "Newsletters"},
                account_label="work@corp.com",
            )
        )
        assert result["id"] == "Label_work@corp.com"
        _, kwargs = next(
            c for c in captured["work@corp.com"] if c[0] == "labels.create"
        )
        assert kwargs["body"]["name"] == "Newsletters"

    def test_modify_message_labels(
        self,
        write_capture_server: tuple[
            types.ModuleType, dict[str, list[tuple[str, dict[str, Any]]]]
        ],
    ) -> None:
        srv, captured = write_capture_server
        client, session_id = _send_capture_client(srv)
        _call_tool(
            client,
            session_id,
            "gmail_modify_message_labels",
            {
                "message_id": "msg1",
                "add_label_ids": ["Label_1"],
                "remove_label_ids": ["UNREAD"],
            },
            account_label="work@corp.com",
        )
        _, kwargs = next(
            c for c in captured["work@corp.com"] if c[0] == "messages.modify"
        )
        assert kwargs["id"] == "msg1"
        assert kwargs["body"]["addLabelIds"] == ["Label_1"]
        assert kwargs["body"]["removeLabelIds"] == ["UNREAD"]

    def test_trash_and_untrash(
        self,
        write_capture_server: tuple[
            types.ModuleType, dict[str, list[tuple[str, dict[str, Any]]]]
        ],
    ) -> None:
        srv, captured = write_capture_server
        client, session_id = _send_capture_client(srv)
        _call_tool(
            client,
            session_id,
            "gmail_trash_message",
            {"message_id": "msg1"},
            account_label="work@corp.com",
        )
        _call_tool(
            client,
            session_id,
            "gmail_untrash_message",
            {"message_id": "msg1"},
            account_label="work@corp.com",
        )
        paths = [p for p, _ in captured["work@corp.com"]]
        assert "messages.trash" in paths
        assert "messages.untrash" in paths
        trash_kwargs = next(
            kw for p, kw in captured["work@corp.com"] if p == "messages.trash"
        )
        assert trash_kwargs["id"] == "msg1"

    def test_writes_target_the_injected_account_not_the_default(
        self,
        write_capture_server: tuple[
            types.ModuleType, dict[str, list[tuple[str, dict[str, Any]]]]
        ],
    ) -> None:
        """A write with the work label must never touch the default (main) service."""
        srv, captured = write_capture_server
        client, session_id = _send_capture_client(srv)
        _call_tool(
            client,
            session_id,
            "gmail_trash_message",
            {"message_id": "msg1"},
            account_label="work@corp.com",
        )
        assert captured["work@corp.com"], "work account recorded no calls"
        assert not captured["main@example.com"], (
            "default account must not be touched when work label is injected"
        )


class TestPermanentDeletesAreHardBlocked:
    """gmail_delete_draft / gmail_delete_label must NOT be registered as tools."""

    def _list_tool_names(self, gmail_server: types.ModuleType) -> set[str]:
        import anyio

        async def _names() -> set[str]:
            tools = await gmail_server.mcp.list_tools()
            return {t.name for t in tools}

        return anyio.run(_names)

    def test_delete_tools_not_registered(
        self, gmail_server: types.ModuleType
    ) -> None:
        names = self._list_tool_names(gmail_server)
        assert "gmail_delete_draft" not in names, (
            "gmail_delete_draft must be hard-blocked — not a registered tool."
        )
        assert "gmail_delete_label" not in names, (
            "gmail_delete_label must be hard-blocked — not a registered tool."
        )

    def test_write_tools_are_registered(
        self, gmail_server: types.ModuleType
    ) -> None:
        names = self._list_tool_names(gmail_server)
        for tool in (
            "gmail_create_draft",
            "gmail_update_draft",
            "gmail_send_draft",
            "gmail_create_label",
            "gmail_modify_message_labels",
            "gmail_trash_message",
            "gmail_untrash_message",
        ):
            assert tool in names, f"{tool} must be a registered server tool."
