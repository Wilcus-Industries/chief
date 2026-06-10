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
    """Load a fresh Gmail server module with two fake service accounts."""
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

    srv._service_registry = {  # type: ignore[attr-defined]
        "main@example.com": main_svc,
        "work@corp.com": work_svc,
    }
    srv._default_service = main_svc  # type: ignore[attr-defined]
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
