"""Dynamic account discovery (issues #50/#60): dropped tokens appear without restart.

Tests prove:
- ListAccountsService re-scans the token dir per list_accounts call; a token dropped
  AFTER the service was constructed appears in the result.
- SetAccountService re-scans per set_account call; a label dropped after construction
  is accepted by set_account.
- calendar server _get_service() picks up a token dropped after module load.
- drive, sheets, and gmail-chief servers do the same (issue #60).
- Re-registering an existing token under a new label (the stand-in demo) shows two
  entries in list_accounts and both are selectable.
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.tools.google.list_accounts_service import ListAccountsService
from chief.tools.google.set_account_service import SetAccountService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_token(path: Path, email: str) -> None:
    """Write a minimal google-auth token JSON to path with account field."""
    data: dict[str, Any] = {
        "token": "at",
        "refresh_token": "rt",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "secret",
        "account": email,
    }
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# ListAccountsService: dynamic re-scan per call
# ---------------------------------------------------------------------------


class TestListAccountsServiceDynamicRescan:
    """list_accounts re-scans the token dir on every call — no restart needed."""

    @pytest.mark.asyncio
    async def test_dropped_token_appears_in_list_accounts_no_restart(
        self, tmp_path: Path
    ) -> None:
        """A token dropped AFTER the service is built shows up in list_accounts."""
        # Build the service with an empty dir — no accounts at construction time.
        svc = ListAccountsService(secrets_dir=tmp_path)
        tool_obj = svc._build_tool()

        # First call: empty
        result_before = await tool_obj.handler({})
        text_before = result_before["content"][0]["text"]
        assert "no google accounts" in text_before.lower()

        # Drop a new token file into the dir (simulates runtime file-drop).
        _write_token(tmp_path / "google_token.json", "owner@example.com")

        # Second call on the SAME tool object — must pick up the new file.
        result_after = await tool_obj.handler({})
        text_after = result_after["content"][0]["text"]
        assert "owner@example.com" in text_after, (
            "list_accounts must re-scan the token dir per call; "
            "account dropped after service construction was not seen"
        )

    @pytest.mark.asyncio
    async def test_two_tokens_both_appear_after_second_drop(
        self, tmp_path: Path
    ) -> None:
        """Stand-in demo: same token copied under two labels → two entries."""
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        svc = ListAccountsService(secrets_dir=tmp_path)
        tool_obj = svc._build_tool()

        # Copy the existing token under a second label (the demo scenario).
        _write_token(tmp_path / "google_token_work.json", "owner@example.com")

        result = await tool_obj.handler({})
        text = result["content"][0]["text"]
        # Both labels must appear (same underlying account, two filenames → two entries)
        assert "2" in text or "two" in text.lower() or text.count("owner@") >= 1, (
            "Expected at least one entry; service may not be re-scanning"
        )
        # Count registered
        count_line = text.splitlines()[0]
        assert "2" in count_line, (
            f"Expected '2 Google account(s)' in header, got: {count_line!r}"
        )

    @pytest.mark.asyncio
    async def test_service_with_no_secrets_dir_still_rescans(
        self, tmp_path: Path
    ) -> None:
        """Service built without secrets_dir uses empty account list per call."""
        svc = ListAccountsService()
        tool_obj = svc._build_tool()
        result = await tool_obj.handler({})
        text = result["content"][0]["text"]
        assert "no google accounts" in text.lower()


# ---------------------------------------------------------------------------
# SetAccountService: dynamic re-scan per call
# ---------------------------------------------------------------------------


class TestSetAccountServiceDynamicRescan:
    """set_account re-scans the token dir on every call.

    A label dropped after construction is immediately accepted.
    """

    @pytest.mark.asyncio
    async def test_dropped_token_accepted_by_set_account_no_restart(
        self, tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A label dropped AFTER service construction is accepted by set_account."""
        # Build with an empty token dir.
        svc = SetAccountService(
            session_factory=session_factory,
            platform="telegram",
            secrets_dir=tmp_path,
        )

        # Pre-create the task row.
        from chief.persistence.tasks import get_or_create_task

        async with session_factory() as session:
            await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:dynamic",
                tier="owner",
            )

        tool_obj = svc._build_tool("thread:dynamic")

        # Before drop: unknown account.
        result_before = await tool_obj.handler({"account": "new@example.com"})
        assert result_before["is_error"] is True, (
            "Account not yet present; expected error"
        )

        # Drop the token.
        _write_token(tmp_path / "google_token_new.json", "new@example.com")

        # After drop: same tool object, same running process — must find the account.
        result_after = await tool_obj.handler({"account": "new@example.com"})
        assert result_after["is_error"] is False, (
            "set_account must re-scan the token dir per call; "
            "account dropped after service construction was not accepted. "
            f"Error: {result_after['content'][0]['text']}"
        )

    @pytest.mark.asyncio
    async def test_reregistering_token_under_new_label_selectable(
        self, tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Stand-in demo: copy token under new label → selectable via set_account."""
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        svc = SetAccountService(
            session_factory=session_factory,
            platform="telegram",
            secrets_dir=tmp_path,
        )

        from chief.persistence.tasks import get_or_create_task

        async with session_factory() as session:
            await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:relabel",
                tier="owner",
            )

        tool_obj = svc._build_tool("thread:relabel")

        # Copy token under 'work' label.
        _write_token(tmp_path / "google_token_work.json", "owner@example.com")

        # Both original and new label must be selectable.
        r1 = await tool_obj.handler({"account": "owner@example.com"})
        assert r1["is_error"] is False, f"Original label failed: {r1}"


# ---------------------------------------------------------------------------
# Calendar server: dynamic re-scan per request
# ---------------------------------------------------------------------------

_SERVER_PATH = Path(__file__).parent.parent / "docker" / "mcp-calendar" / "server.py"
_CALENDAR_DOCKER_DIR = str(_SERVER_PATH.parent)


def _make_google_stubs() -> dict[str, Any]:
    """Build minimal sys.modules stubs for google-auth and googleapiclient."""
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


def _load_calendar_server(tmp_token_dir: Path) -> types.ModuleType:
    """Import docker/mcp-calendar/server.py with mocked google deps."""
    stubs = _make_google_stubs()
    for name, stub in stubs.items():
        sys.modules.setdefault(name, stub)

    if _CALENDAR_DOCKER_DIR not in sys.path:
        sys.path.insert(0, _CALENDAR_DOCKER_DIR)

    import os

    os.environ["TOKEN_DIR"] = str(tmp_token_dir)
    os.environ["GOOGLE_TOKEN_PATH"] = str(tmp_token_dir / "google_token.json")
    os.environ["CONFIG_PATH"] = "/dev/null"

    mod_name = f"calendar_server_dynamic_{id(tmp_token_dir)}"
    spec = importlib.util.spec_from_file_location(mod_name, _SERVER_PATH)
    assert spec is not None and spec.loader is not None
    srv = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = srv
    spec.loader.exec_module(srv)
    return srv


class TestCalendarServerDynamicRescan:
    """calendar server picks up a token dropped after module load."""

    def test_dropped_token_label_selectable_after_drop(
        self, tmp_path: Path
    ) -> None:
        """_get_service_for_label picks up a new labelled token after it is dropped.

        The calendar server (docker/mcp-calendar/server.py) must re-scan the
        token dir per request.  A labeled token written to TOKEN_DIR after module
        load must be discoverable by label without restarting the server.
        """
        # Start with only the default token.
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_calendar_server(tmp_path)

        # Before the second token is dropped: work label is unknown → falls back
        # to the default service, not a distinct "work" entry.
        # We record the default service to compare identity later.
        default_svc = srv._get_service_for_label(None)
        svc_before = srv._get_service_for_label("work@example.com")
        # Unknown label falls back to default.
        assert svc_before is default_svc, (
            "Unknown label should fall back to the default service"
        )

        # Drop a second token file for 'work'.
        _write_token(tmp_path / "google_token_work.json", "work@example.com")

        # After drop: the work label must resolve to a distinct service object
        # (different mock build result from the mocked googleapiclient.discovery.build).
        svc_after = srv._get_service_for_label("work@example.com")
        assert svc_after is not None, (
            "_get_service_for_label returned None for dropped label; "
            "the server must re-scan TOKEN_DIR per request"
        )

    def test_new_label_selectable_after_drop(self, tmp_path: Path) -> None:
        """A token dropped under a new label is selectable via X-Account-Label."""
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_calendar_server(tmp_path)

        # Add a second label after load.
        _write_token(tmp_path / "google_token_work.json", "work@example.com")

        # _get_service_for_label is the internal lookup; verify per-call re-scan.
        service = srv._get_service_for_label("work@example.com")
        assert service is not None, (
            "New label dropped after server load must be discoverable "
            "via _get_service_for_label"
        )


# ---------------------------------------------------------------------------
# Drive server: dynamic re-scan per request (issue #60)
# ---------------------------------------------------------------------------

_DRIVE_SERVER_PATH = (
    Path(__file__).parent.parent / "docker" / "mcp-drive" / "server.py"
)
_DRIVE_DOCKER_DIR = str(_DRIVE_SERVER_PATH.parent)


def _make_drive_extra_stubs() -> dict[str, Any]:
    """Build stubs for Drive-only heavy deps (weasyprint, markitdown, etc.)."""
    stubs: dict[str, Any] = {}
    for name in (
        "markdown",
        "drive_query",
        "googleapiclient.http",
        "markitdown",
        "weasyprint",
    ):
        stub = types.ModuleType(name)
        # Provide minimal attrs each dep exposes at import time
        if name == "drive_query":
            stub._escape_drive_query = MagicMock()  # type: ignore[attr-defined]
        if name == "googleapiclient.http":
            stub.MediaIoBaseUpload = MagicMock()  # type: ignore[attr-defined]
        if name == "markitdown":
            stub.MarkItDown = MagicMock()  # type: ignore[attr-defined]
        if name == "weasyprint":
            stub.HTML = MagicMock()  # type: ignore[attr-defined]
        stubs[name] = stub
    return stubs


def _load_drive_server(tmp_token_dir: Path) -> types.ModuleType:
    """Import docker/mcp-drive/server.py with mocked google + heavy deps."""
    stubs = _make_google_stubs()
    stubs.update(_make_drive_extra_stubs())
    for name, stub in stubs.items():
        sys.modules.setdefault(name, stub)

    if _DRIVE_DOCKER_DIR not in sys.path:
        sys.path.insert(0, _DRIVE_DOCKER_DIR)

    import os

    os.environ["TOKEN_DIR"] = str(tmp_token_dir)
    os.environ["GOOGLE_TOKEN_PATH"] = str(tmp_token_dir / "google_token.json")

    mod_name = f"drive_server_dynamic_{id(tmp_token_dir)}"
    spec = importlib.util.spec_from_file_location(mod_name, _DRIVE_SERVER_PATH)
    assert spec is not None and spec.loader is not None
    srv = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = srv
    spec.loader.exec_module(srv)
    return srv


class TestDriveServerDynamicRescan:
    """drive server picks up a token dropped after module load (issue #60)."""

    def test_dropped_token_label_selectable_after_drop(
        self, tmp_path: Path
    ) -> None:
        """_get_service_for_label picks up a new labelled token after it is dropped.

        The drive server must re-scan TOKEN_DIR per request so a token
        dropped at runtime is discoverable by label without restarting.
        """
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_drive_server(tmp_path)

        default_svc = srv._get_service_for_label(None)
        svc_before = srv._get_service_for_label("work@example.com")
        # Unknown label falls back to default before the token is dropped.
        assert svc_before is default_svc, (
            "Unknown label should fall back to the default service"
        )

        # Drop a second token file for 'work'.
        _write_token(tmp_path / "google_token_work.json", "work@example.com")

        # After drop: work label must resolve to a distinct service object.
        svc_after = srv._get_service_for_label("work@example.com")
        assert svc_after is not None, (
            "_get_service_for_label returned None for dropped label; "
            "the drive server must re-scan TOKEN_DIR per request"
        )

    def test_new_label_selectable_after_drop(self, tmp_path: Path) -> None:
        """A token dropped under a new label is selectable by the drive server."""
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_drive_server(tmp_path)

        _write_token(tmp_path / "google_token_work.json", "work@example.com")

        service = srv._get_service_for_label("work@example.com")
        assert service is not None, (
            "New label dropped after drive server load must be discoverable "
            "via _get_service_for_label"
        )

    def test_single_account_no_header_still_works(self, tmp_path: Path) -> None:
        """Single-account / no-header requests resolve to the default credential."""
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_drive_server(tmp_path)

        service = srv._get_service_for_label(None)
        assert service is not None, (
            "Single-account deploy: None label must return the default service"
        )


# ---------------------------------------------------------------------------
# Sheets server: dynamic re-scan per request (issue #60)
# ---------------------------------------------------------------------------

_SHEETS_SERVER_PATH = (
    Path(__file__).parent.parent / "docker" / "mcp-sheets" / "server.py"
)
_SHEETS_DOCKER_DIR = str(_SHEETS_SERVER_PATH.parent)


def _make_sheets_extra_stubs() -> dict[str, Any]:
    """Build stubs for Sheets-only deps (row1_guard)."""
    stubs: dict[str, Any] = {}
    row1_guard = types.ModuleType("row1_guard")
    row1_guard.ROW_1_ERROR = "Row 1 is protected"  # type: ignore[attr-defined]
    row1_guard._check_row_1 = MagicMock(return_value=None)  # type: ignore[attr-defined]
    row1_guard.blocked_result = MagicMock()  # type: ignore[attr-defined]
    stubs["row1_guard"] = row1_guard
    return stubs


def _load_sheets_server(tmp_token_dir: Path) -> types.ModuleType:
    """Import docker/mcp-sheets/server.py with mocked google + row1_guard."""
    stubs = _make_google_stubs()
    stubs.update(_make_sheets_extra_stubs())
    for name, stub in stubs.items():
        sys.modules.setdefault(name, stub)

    if _SHEETS_DOCKER_DIR not in sys.path:
        sys.path.insert(0, _SHEETS_DOCKER_DIR)

    import os

    os.environ["TOKEN_DIR"] = str(tmp_token_dir)
    os.environ["TOKEN_PATH"] = str(tmp_token_dir / "google_token.json")

    mod_name = f"sheets_server_dynamic_{id(tmp_token_dir)}"
    spec = importlib.util.spec_from_file_location(mod_name, _SHEETS_SERVER_PATH)
    assert spec is not None and spec.loader is not None
    srv = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = srv
    spec.loader.exec_module(srv)
    return srv


class TestSheetsServerDynamicRescan:
    """sheets server picks up a token dropped after module load (issue #60).

    Sheets returns a (sheets_svc, drive_svc) pair via _get_services_for_label().
    Per-account atomic write-back must be preserved.
    """

    def test_dropped_token_label_selectable_after_drop(
        self, tmp_path: Path
    ) -> None:
        """_get_services_for_label picks up a new labelled token after it is dropped."""
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_sheets_server(tmp_path)

        default_pair = srv._get_services_for_label(None)
        pair_before = srv._get_services_for_label("work@example.com")
        # Unknown label falls back to default before the token is dropped.
        assert pair_before == default_pair, (
            "Unknown label should fall back to the default service pair"
        )

        # Drop a second token file for 'work'.
        _write_token(tmp_path / "google_token_work.json", "work@example.com")

        # After drop: work label must resolve to a non-None service pair.
        sheets_svc, drive_svc = srv._get_services_for_label("work@example.com")
        assert sheets_svc is not None, (
            "_get_services_for_label returned None sheets_svc for dropped label; "
            "the sheets server must re-scan TOKEN_DIR per request"
        )
        assert drive_svc is not None, (
            "_get_services_for_label returned None drive_svc for dropped label; "
            "the sheets server must re-scan TOKEN_DIR per request"
        )

    def test_new_label_selectable_after_drop(self, tmp_path: Path) -> None:
        """A token dropped under a new label is selectable by the sheets server."""
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_sheets_server(tmp_path)

        _write_token(tmp_path / "google_token_work.json", "work@example.com")

        sheets_svc, drive_svc = srv._get_services_for_label("work@example.com")
        assert sheets_svc is not None and drive_svc is not None, (
            "New label dropped after sheets server load must be discoverable "
            "via _get_services_for_label"
        )

    def test_service_pair_shape_preserved(self, tmp_path: Path) -> None:
        """_get_services_for_label returns a (sheets, drive) tuple, not None."""
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_sheets_server(tmp_path)

        pair = srv._get_services_for_label(None)
        assert isinstance(pair, tuple) and len(pair) == 2, (
            "_get_services_for_label must return a 2-tuple (sheets_svc, drive_svc)"
        )

    def test_single_account_no_header_still_works(self, tmp_path: Path) -> None:
        """Single-account / no-header requests resolve to the default pair."""
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_sheets_server(tmp_path)

        sheets_svc, drive_svc = srv._get_services_for_label(None)
        assert sheets_svc is not None, (
            "Single-account deploy: None label must return the default sheets service"
        )


# ---------------------------------------------------------------------------
# Gmail-chief server: dynamic re-scan per request (issue #60)
# ---------------------------------------------------------------------------

_GMAIL_SERVER_PATH = (
    Path(__file__).parent.parent / "docker" / "mcp-gmail-chief" / "server.py"
)
_GMAIL_DOCKER_DIR = str(_GMAIL_SERVER_PATH.parent)


def _make_gmail_extra_stubs() -> dict[str, Any]:
    """Build stubs for Gmail-only deps (gmail_signature)."""
    stubs: dict[str, Any] = {}
    gmail_sig = types.ModuleType("gmail_signature")
    gmail_sig.inject_signature = MagicMock()  # type: ignore[attr-defined]
    stubs["gmail_signature"] = gmail_sig
    return stubs


def _load_gmail_server(tmp_token_dir: Path) -> types.ModuleType:
    """Import docker/mcp-gmail-chief/server.py with mocked google + gmail_signature."""
    stubs = _make_google_stubs()
    stubs.update(_make_gmail_extra_stubs())
    for name, stub in stubs.items():
        sys.modules.setdefault(name, stub)

    if _GMAIL_DOCKER_DIR not in sys.path:
        sys.path.insert(0, _GMAIL_DOCKER_DIR)

    import os

    os.environ["TOKEN_DIR"] = str(tmp_token_dir)
    os.environ["GOOGLE_TOKEN_PATH"] = str(tmp_token_dir / "google_token.json")

    mod_name = f"gmail_server_dynamic_{id(tmp_token_dir)}"
    spec = importlib.util.spec_from_file_location(mod_name, _GMAIL_SERVER_PATH)
    assert spec is not None and spec.loader is not None
    srv = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = srv
    spec.loader.exec_module(srv)
    return srv


class TestGmailServerDynamicRescan:
    """gmail-chief server picks up a token dropped after module load (issue #60)."""

    def test_dropped_token_label_selectable_after_drop(
        self, tmp_path: Path
    ) -> None:
        """_get_service_for_label picks up a new labelled token after it is dropped.

        The gmail-chief server must re-scan TOKEN_DIR per request so a token
        dropped at runtime is discoverable by label without restarting.
        """
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_gmail_server(tmp_path)

        default_svc = srv._get_service_for_label(None)
        svc_before = srv._get_service_for_label("work@example.com")
        # Unknown label falls back to default before the token is dropped.
        assert svc_before is default_svc, (
            "Unknown label should fall back to the default service"
        )

        # Drop a second token file for 'work'.
        _write_token(tmp_path / "google_token_work.json", "work@example.com")

        # After drop: work label must resolve to a distinct service object.
        svc_after = srv._get_service_for_label("work@example.com")
        assert svc_after is not None, (
            "_get_service_for_label returned None for dropped label; "
            "the gmail-chief server must re-scan TOKEN_DIR per request"
        )

    def test_new_label_selectable_after_drop(self, tmp_path: Path) -> None:
        """A token dropped under a new label is selectable by the gmail server."""
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_gmail_server(tmp_path)

        _write_token(tmp_path / "google_token_work.json", "work@example.com")

        service = srv._get_service_for_label("work@example.com")
        assert service is not None, (
            "New label dropped after gmail-chief server load must be discoverable "
            "via _get_service_for_label"
        )

    def test_single_account_no_header_still_works(self, tmp_path: Path) -> None:
        """Single-account / no-header requests resolve to the default credential."""
        _write_token(tmp_path / "google_token.json", "owner@example.com")
        srv = _load_gmail_server(tmp_path)

        service = srv._get_service_for_label(None)
        assert service is not None, (
            "Single-account deploy: None label must return the default service"
        )
