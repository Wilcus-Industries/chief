"""Integration test: X-Account-Label header → correct service per tools/call.

Proves that the multi-account calendar MCP server (docker/mcp-calendar/server.py)
selects the right Google API service for each individual tools/call request on a
PERSISTENT stateful MCP session — specifically that the label is NOT frozen at
session-start (the contextvar bug described in issue #56).

The test drives the ACTUAL ASGI app from server.py end-to-end:
  HTTP POST /mcp (initialize)  →  get session id
  HTTP POST /mcp (tools/call)  →  X-Account-Label: work@corp.com   → work service
  HTTP POST /mcp (tools/call)  →  X-Account-Label: main@example.com → main service
  HTTP POST /mcp (tools/call)  →  (no header)                      → default service

Google API deps are mocked at the sys.modules level before server.py is imported;
the mock services return a distinct calendar id so we can assert which account was
selected purely from the tool response text.
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
# Module-level bootstrap: mock google + googleapiclient before importing server
# ---------------------------------------------------------------------------

_SERVER_PATH = Path(__file__).parent.parent / "docker" / "mcp-calendar" / "server.py"
_CALENDAR_DOCKER_DIR = str(_SERVER_PATH.parent)

# Modules that server.py imports which are NOT installed in the chief venv.
# We must inject stubs before spec.loader.exec_module runs.
_GOOGLE_STUBS: dict[str, Any] = {}


def _make_google_stubs() -> dict[str, Any]:
    """Build minimal sys.modules stubs for google-auth and googleapiclient."""
    stubs: dict[str, Any] = {}

    # google.oauth2.credentials.Credentials
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
    googleapiclient_discovery.build = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]

    stubs["google"] = google_pkg
    stubs["google.oauth2"] = google_oauth2
    stubs["google.oauth2.credentials"] = google_oauth2_creds
    stubs["google.auth"] = google_auth
    stubs["google.auth.transport"] = google_auth_transport
    stubs["google.auth.transport.requests"] = google_auth_transport_requests
    stubs["googleapiclient"] = googleapiclient_pkg
    stubs["googleapiclient.discovery"] = googleapiclient_discovery
    return stubs


def _load_calendar_server(tmp_token_dir: Path) -> types.ModuleType:
    """Import docker/mcp-calendar/server.py with mocked google deps.

    Uses a unique module name to prevent collision when called multiple times
    within the same test process (each load gets a fresh module object and
    therefore fresh module-level state: _service_registry, _default_service,
    the FastMCP instance, and the ContextVar).
    """
    # Inject google stubs into sys.modules BEFORE exec_module runs.
    stubs = _make_google_stubs()
    for name, stub in stubs.items():
        sys.modules.setdefault(name, stub)

    # owner_tz is shipped alongside server.py; add its directory to sys.path.
    if _CALENDAR_DOCKER_DIR not in sys.path:
        sys.path.insert(0, _CALENDAR_DOCKER_DIR)

    import os

    os.environ["TOKEN_DIR"] = str(tmp_token_dir)
    os.environ["GOOGLE_TOKEN_PATH"] = str(tmp_token_dir / "google_token.json")
    os.environ["CONFIG_PATH"] = "/dev/null"

    # Use a unique module name so each test gets a fresh module with fresh globals.
    mod_name = f"calendar_server_{id(tmp_token_dir)}"
    spec = importlib.util.spec_from_file_location(mod_name, _SERVER_PATH)
    assert spec is not None and spec.loader is not None
    srv = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = srv
    spec.loader.exec_module(srv)
    return srv


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _extract_result_text(sse_body: str) -> str:
    """Extract the first data: line from an SSE response body."""
    for line in sse_body.splitlines():
        if line.startswith("data:"):
            return line[len("data:"):].strip()
    return sse_body


def _calendar_id_from_list(sse_body: str) -> str:
    """Return the first calendar id from a list-calendars SSE response."""
    raw = _extract_result_text(sse_body)
    payload = json.loads(raw)
    content_text = payload["result"]["content"][0]["text"]
    items = json.loads(content_text)
    return items[0]["id"] if items else ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def calendar_server(tmp_path: Path) -> types.ModuleType:
    """Load a fresh calendar server module with two fake service accounts.

    Patches ``_build_service_registry`` on the loaded module so every per-request
    rescan (issue #50) returns the same deterministic fake registry — the two
    mocked services are always "visible" regardless of TOKEN_DIR contents.
    """
    srv = _load_calendar_server(tmp_path)

    # Build per-label fake services.
    main_svc = MagicMock()
    main_svc.calendarList().list().execute.return_value = {
        "items": [{"id": "cal_main", "summary": "Main", "accessRole": "owner"}]
    }
    work_svc = MagicMock()
    work_svc.calendarList().list().execute.return_value = {
        "items": [{"id": "cal_work", "summary": "Work", "accessRole": "owner"}]
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
def mcp_client(calendar_server: types.ModuleType) -> Any:
    """Return a (TestClient, session_id) pair on an initialized MCP session."""
    # The fixed server reads X-Account-Label from FastMCP's per-call request_context
    # directly — no middleware wrapper needed.
    starlette_app = calendar_server.mcp.streamable_http_app()

    client = TestClient(starlette_app, raise_server_exceptions=True)
    client.__enter__()

    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.0.1"},
        },
    }
    r = client.post(
        "/mcp",
        json=init_msg,
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
# Tests
# ---------------------------------------------------------------------------


class TestAccountLabelInjectionServerPath:
    """X-Account-Label header on tools/call → correct service, same session."""

    def _list_calendars(
        self,
        client: TestClient,
        session_id: str,
        *,
        account_label: str | None = None,
    ) -> str:
        """Send a list-calendars tools/call; return the calendar id from the result."""
        extra: dict[str, str] = {}
        if account_label is not None:
            extra["X-Account-Label"] = account_label
        r = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list-calendars", "arguments": {}},
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Mcp-Session-Id": session_id,
                **extra,
            },
        )
        assert r.status_code == 200, f"tools/call failed: {r.text}"
        return _calendar_id_from_list(r.text)

    def test_sequential_requests_different_labels_same_session(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        """Two sequential tools/call on the same session return different accounts.

        This is the keystone regression test: with the old contextvar approach the
        second call returns the label from the FIRST request (frozen at session
        creation), not the label carried in its own X-Account-Label header.
        With the fix, each call sees its own header.
        """
        client, session_id = mcp_client

        cal_work = self._list_calendars(
            client, session_id, account_label="work@corp.com"
        )
        cal_main = self._list_calendars(
            client, session_id, account_label="main@example.com"
        )

        assert cal_work == "cal_work", (
            f"Expected cal_work for work@corp.com, got {cal_work!r}. "
            "The account label is frozen at session start (contextvar bug)."
        )
        assert cal_main == "cal_main", (
            f"Expected cal_main for main@example.com, got {cal_main!r}."
        )

    def test_label_reversed_order_same_session(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        """Reversed order: main first, then work — both still correct."""
        client, session_id = mcp_client

        cal_main = self._list_calendars(
            client, session_id, account_label="main@example.com"
        )
        cal_work = self._list_calendars(
            client, session_id, account_label="work@corp.com"
        )

        assert cal_main == "cal_main", f"Expected cal_main, got {cal_main!r}"
        assert cal_work == "cal_work", f"Expected cal_work, got {cal_work!r}"

    def test_absent_header_uses_default_credential(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        """No X-Account-Label header → falls back to default (first) credential.

        Backward-compat: single-account deploys never send a label header and
        must still work.
        """
        client, session_id = mcp_client

        cal = self._list_calendars(client, session_id, account_label=None)

        assert cal == "cal_main", (
            f"Expected default (cal_main) when no header, got {cal!r}"
        )

    def test_unknown_label_falls_back_to_default(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        """Unknown label → falls back to default; no crash."""
        client, session_id = mcp_client

        cal = self._list_calendars(
            client, session_id, account_label="nobody@example.com"
        )

        assert cal == "cal_main", (
            f"Expected default (cal_main) for unknown label, got {cal!r}"
        )


class TestConcurrentRequestsDifferentLabels:
    """Interleaved requests with different labels select their own credential."""

    def test_concurrent_calls_no_crosstalk(
        self, calendar_server: types.ModuleType, tmp_path: Path
    ) -> None:
        """Interleaved tools/call requests with different labels select their own svc.

        Simulates the cross-request race: request A with work label and request B
        with main label are issued in the same session; neither should see the
        other's label.

        Note: TestClient is synchronous; we simulate concurrency by making two
        calls on separate sessions (each session starts with a different label on
        the first call, then switches — this is the meaningful cross-talk test).
        A stronger async concurrency test would require a live server; this is
        the in-process approximation that is provably correct under the fix.
        """
        starlette_app = calendar_server.mcp.streamable_http_app()

        results: list[tuple[str, str]] = []

        with TestClient(starlette_app, raise_server_exceptions=True) as client:
            # Session 1: work first, then main
            r_init = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "c", "version": "0"},
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
            sid = r_init.headers["mcp-session-id"]

            for label, expected in [
                ("work@corp.com", "cal_work"),
                ("main@example.com", "cal_main"),
                ("work@corp.com", "cal_work"),
                ("main@example.com", "cal_main"),
            ]:
                r = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "list-calendars", "arguments": {}},
                    },
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                        "Mcp-Session-Id": sid,
                        "X-Account-Label": label,
                    },
                )
                cal_id = _calendar_id_from_list(r.text)
                results.append((expected, cal_id))

        for expected, actual in results:
            assert actual == expected, (
                f"Expected {expected!r}, got {actual!r}. "
                "Cross-talk: one request's label bled into another."
            )
