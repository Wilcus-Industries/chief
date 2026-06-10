"""Server-level regression test: row-1 guard refusal survives MCP output validation.

Under mcp 1.27.2 a bare content-list dict (``{"isError": True, "content": [...]}``)
returned from a guarded tool is treated as "no structured output" and — because the tool
declares a ``dict[str, Any]`` return type, causing FastMCP to emit an ``outputSchema`` —
gets replaced by a generic ``"Output validation error: ..."``, losing ``ROW_1_ERROR``.

Returning the ``CallToolResult`` from ``_guard()`` short-circuits the lowlevel handler
so no output validation runs; the error message is delivered verbatim.  This test drives
every guarded tool through the real ASGI transport and asserts that ``ROW_1_ERROR`` text
reaches the caller.
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
    Path(__file__).parent.parent / "docker" / "mcp-sheets" / "server.py"
)
_SERVER_DIR = str(_SERVER_PATH.parent)

# ---------------------------------------------------------------------------
# Google / googleapiclient stubs (same pattern as test_gmail_server.py)
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
    google_auth_transport_requests = types.ModuleType(
        "google.auth.transport.requests"
    )
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


def _load_sheets_server(tmp_token_dir: Path) -> types.ModuleType:
    """Import docker/mcp-sheets/server.py with mocked Google deps."""
    stubs = _make_google_stubs()
    for name, stub in stubs.items():
        sys.modules.setdefault(name, stub)

    if _SERVER_DIR not in sys.path:
        sys.path.insert(0, _SERVER_DIR)

    # Load the real row1_guard from _SERVER_DIR, overriding any mock that a
    # prior test (e.g. test_dynamic_account_discovery) may have registered via
    # sys.modules.setdefault.  The guard must be the real implementation so the
    # row-1 checks actually fire in these transport-level tests.
    _guard_path = Path(_SERVER_DIR) / "row1_guard.py"
    _guard_spec = importlib.util.spec_from_file_location("row1_guard", _guard_path)
    assert _guard_spec is not None and _guard_spec.loader is not None
    _guard_mod = importlib.util.module_from_spec(_guard_spec)
    sys.modules["row1_guard"] = _guard_mod
    _guard_spec.loader.exec_module(_guard_mod)

    import os

    os.environ["TOKEN_DIR"] = str(tmp_token_dir)
    os.environ["TOKEN_PATH"] = str(tmp_token_dir / "google_token.json")

    mod_name = f"sheets_server_{id(tmp_token_dir)}"
    spec = importlib.util.spec_from_file_location(mod_name, _SERVER_PATH)
    assert spec is not None and spec.loader is not None
    srv = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = srv
    spec.loader.exec_module(srv)
    return srv


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sheets_server(tmp_path: Path) -> types.ModuleType:
    """Load a fresh Sheets server module with a stub service registry."""
    srv = _load_sheets_server(tmp_path)
    # No real Google API calls — the guard fires before _get_services() is reached,
    # so we just need _service_registry to be non-empty so _default_services resolves.
    stub_svc = MagicMock()
    srv._service_registry = {"main@example.com": (stub_svc, stub_svc)}  # type: ignore[attr-defined]
    srv._default_services = (stub_svc, stub_svc)  # type: ignore[attr-defined]
    return srv


@pytest.fixture()
def mcp_client(sheets_server: types.ModuleType) -> Any:
    """Return a (TestClient, session_id) pair on an initialized MCP session."""
    starlette_app = sheets_server.mcp.streamable_http_app()
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

_ROW_1_ERROR_FRAGMENT = "You cannot edit the first row"


def _call_tool(
    client: TestClient,
    session_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Send a tools/call and return the decoded JSON-RPC response payload."""
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Session-Id": session_id,
        },
    )
    assert r.status_code == 200, f"tools/call failed: {r.text}"
    # SSE: find first data: line
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())  # type: ignore[no-any-return]
    raise AssertionError(f"No data: line in SSE response: {r.text}")


def _assert_row1_refusal(payload: dict[str, Any], tool_name: str) -> None:
    """Assert the MCP response carries ROW_1_ERROR and isError=True."""
    result = payload.get("result", {})
    assert result.get("isError") is True, (
        f"{tool_name}: expected isError=True, got payload={payload}"
    )
    content = result.get("content", [])
    assert content, f"{tool_name}: content list is empty, payload={payload}"
    text = content[0].get("text", "")
    assert _ROW_1_ERROR_FRAGMENT in text, (
        f"{tool_name}: ROW_1_ERROR not in response text {text!r}, "
        f"payload={payload}"
    )


# ---------------------------------------------------------------------------
# Tests: ROW_1_ERROR survives MCP output validation for each guarded tool
# ---------------------------------------------------------------------------


class TestSheetsRow1GuardTransport:
    """Row-1 refusal message survives the full MCP transport (not just guard logic).

    Each test drives a row-1-touching call through the ASGI stack and asserts that
    the response carries ROW_1_ERROR text.  If the server returned a bare dict the
    mcp 1.27.2 lowlevel handler would replace it with "Output validation error: ...",
    causing these assertions to fail.
    """

    def test_update_cells_row1_blocked(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        client, session_id = mcp_client
        payload = _call_tool(
            client,
            session_id,
            "update_cells",
            {
                "spreadsheet_id": "fake-id",
                "sheet": "Sheet1",
                "range": "A1:B2",
                "data": [["header"]],
            },
        )
        _assert_row1_refusal(payload, "update_cells")

    def test_batch_update_cells_row1_blocked(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        client, session_id = mcp_client
        payload = _call_tool(
            client,
            session_id,
            "batch_update_cells",
            {
                "spreadsheet_id": "fake-id",
                "sheet": "Sheet1",
                "ranges": {"A1:B2": [["header"]]},
            },
        )
        _assert_row1_refusal(payload, "batch_update_cells")

    def test_add_rows_row1_blocked(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        client, session_id = mcp_client
        payload = _call_tool(
            client,
            session_id,
            "add_rows",
            {
                "spreadsheet_id": "fake-id",
                "sheet": "Sheet1",
                "count": 1,
                "start_row": 0,
            },
        )
        _assert_row1_refusal(payload, "add_rows")

    def test_batch_update_row1_blocked(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        client, session_id = mcp_client
        payload = _call_tool(
            client,
            session_id,
            "batch_update",
            {
                "spreadsheet_id": "fake-id",
                "requests": [
                    {"updateCells": {"range": {"startRowIndex": 0}}}
                ],
            },
        )
        _assert_row1_refusal(payload, "batch_update")

    def test_add_chart_row1_blocked(
        self, mcp_client: tuple[TestClient, str]
    ) -> None:
        client, session_id = mcp_client
        payload = _call_tool(
            client,
            session_id,
            "add_chart",
            {
                "spreadsheet_id": "fake-id",
                "sheet": "Sheet1",
                "chart_type": "COLUMN",
                "data_range": "A1:B10",
            },
        )
        _assert_row1_refusal(payload, "add_chart")
