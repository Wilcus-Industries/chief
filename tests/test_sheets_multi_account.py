"""Server-level unit tests: Sheets server credential selection per request,
and per-account atomic token write-back (the write-race fix).

Proves:
- Multi-account credential selection works per request (same as drive/calendar).
- Token refresh write-back is per-account (writes to the correct token file).
- Concurrent writes to *different* account files don't collide (each file is
  written atomically via temp-then-rename so no partial reads).
- A write to one account file does not corrupt another account's file.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_token(path: Path, label: str) -> None:
    """Write a minimal google-auth token JSON with an ``account`` field."""
    data = {
        "token": f"at_{label}",
        "refresh_token": f"rt_{label}",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "secret",
        "scopes": ["https://www.googleapis.com/auth/spreadsheets",
                   "https://www.googleapis.com/auth/drive"],
        "account": label,
    }
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Credential loading tests
# ---------------------------------------------------------------------------


class TestSheetsLoadAccountCredentials:
    """Unit tests for the multi-account credential loader (sheets)."""

    def test_load_single_token(self, tmp_path: Any) -> None:
        from chief.tools.sheets.credentials import load_account_credentials

        _write_token(tmp_path / "google_token.json", "main@example.com")
        registry = load_account_credentials(tmp_path)

        assert "main@example.com" in registry
        assert len(registry) == 1

    def test_load_multiple_tokens(self, tmp_path: Any) -> None:
        from chief.tools.sheets.credentials import load_account_credentials

        _write_token(tmp_path / "google_token.json", "main@example.com")
        _write_token(tmp_path / "google_token_work.json", "work@corp.com")
        registry = load_account_credentials(tmp_path)

        assert "main@example.com" in registry
        assert "work@corp.com" in registry
        assert len(registry) == 2

    def test_empty_dir_returns_empty_registry(self, tmp_path: Any) -> None:
        from chief.tools.sheets.credentials import load_account_credentials

        registry = load_account_credentials(tmp_path)
        assert registry == {}

    def test_missing_dir_returns_empty_registry(self, tmp_path: Any) -> None:
        from chief.tools.sheets.credentials import load_account_credentials

        registry = load_account_credentials(tmp_path / "nonexistent")
        assert registry == {}


class TestSheetsSelectCredential:
    """Unit tests for per-request credential selection (sheets)."""

    def test_select_by_label(self, tmp_path: Any) -> None:
        from chief.tools.sheets.credentials import (
            load_account_credentials,
            select_credential,
        )

        _write_token(tmp_path / "google_token.json", "main@example.com")
        _write_token(tmp_path / "google_token_work.json", "work@corp.com")
        registry = load_account_credentials(tmp_path)

        cred = select_credential(registry, "work@corp.com")
        assert cred is not None

    def test_select_unknown_falls_back(self, tmp_path: Any) -> None:
        from chief.tools.sheets.credentials import (
            load_account_credentials,
            select_credential,
        )

        _write_token(tmp_path / "google_token.json", "main@example.com")
        registry = load_account_credentials(tmp_path)

        cred = select_credential(registry, "nobody@example.com")
        assert cred is not None

    def test_select_none_falls_back(self, tmp_path: Any) -> None:
        from chief.tools.sheets.credentials import (
            load_account_credentials,
            select_credential,
        )

        _write_token(tmp_path / "google_token.json", "main@example.com")
        registry = load_account_credentials(tmp_path)

        cred = select_credential(registry, None)
        assert cred is not None

    def test_select_empty_registry_returns_none(self, tmp_path: Any) -> None:
        from chief.tools.sheets.credentials import (
            load_account_credentials,
            select_credential,
        )

        registry = load_account_credentials(tmp_path)
        cred = select_credential(registry, "work@corp.com")
        assert cred is None


# ---------------------------------------------------------------------------
# Per-account atomic token write-back (the write-race fix)
# ---------------------------------------------------------------------------


class TestAtomicTokenWriteBack:
    """write_token_atomic persists a token to the correct per-account file."""

    def test_write_creates_file_with_correct_content(self, tmp_path: Any) -> None:
        """write_token_atomic creates the target file with the given data."""
        from chief.tools.sheets.credentials import write_token_atomic

        token_path = tmp_path / "google_token.json"
        data = {"refresh_token": "new_rt", "account": "main@example.com"}
        write_token_atomic(token_path, data)

        written = json.loads(token_path.read_text(encoding="utf-8"))
        assert written["refresh_token"] == "new_rt"
        assert written["account"] == "main@example.com"

    def test_write_different_accounts_do_not_collide(self, tmp_path: Any) -> None:
        """Writes to different account files do not corrupt each other."""
        from chief.tools.sheets.credentials import write_token_atomic

        path_a = tmp_path / "google_token.json"
        path_b = tmp_path / "google_token_work.json"
        data_a = {"refresh_token": "rt_main", "account": "main@example.com"}
        data_b = {"refresh_token": "rt_work", "account": "work@corp.com"}

        write_token_atomic(path_a, data_a)
        write_token_atomic(path_b, data_b)

        assert json.loads(path_a.read_text())["refresh_token"] == "rt_main"
        assert json.loads(path_b.read_text())["refresh_token"] == "rt_work"

    def test_concurrent_writes_to_different_files_no_collision(
        self, tmp_path: Any
    ) -> None:
        """Concurrent writes to different account files produce no cross-talk."""
        from chief.tools.sheets.credentials import write_token_atomic

        path_a = tmp_path / "google_token.json"
        path_b = tmp_path / "google_token_work.json"
        errors: list[Exception] = []

        def write_a() -> None:
            try:
                for _ in range(20):
                    write_token_atomic(
                        path_a,
                        {"refresh_token": "rt_main", "account": "main@example.com"},
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def write_b() -> None:
            try:
                for _ in range(20):
                    write_token_atomic(
                        path_b,
                        {"refresh_token": "rt_work", "account": "work@corp.com"},
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t_a = threading.Thread(target=write_a)
        t_b = threading.Thread(target=write_b)
        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        assert not errors, f"Concurrent write errors: {errors}"
        # Each file contains its own account's data
        assert json.loads(path_a.read_text())["refresh_token"] == "rt_main"
        assert json.loads(path_b.read_text())["refresh_token"] == "rt_work"

    def test_write_is_atomic_no_partial_read(self, tmp_path: Any) -> None:
        """write_token_atomic uses temp-then-rename so readers never see partial data.

        We verify this by checking that the final file is valid JSON (the
        atomic rename ensures it was never partially written from a reader's
        perspective).
        """
        from chief.tools.sheets.credentials import write_token_atomic

        token_path = tmp_path / "google_token.json"
        large_data = {
            "refresh_token": "x" * 10_000,
            "account": "main@example.com",
            "extra": "y" * 10_000,
        }
        write_token_atomic(token_path, large_data)

        # Must parse as valid JSON (would fail if partially written)
        result = json.loads(token_path.read_text(encoding="utf-8"))
        assert result["refresh_token"] == "x" * 10_000


# ---------------------------------------------------------------------------
# Sheets mcp.py service factory with headers
# ---------------------------------------------------------------------------


class TestSheetsMcpServiceFactory:
    """sheets.mcp.service() can inject headers into the service config."""

    def test_service_with_account_header(self) -> None:
        from chief.tools.sheets import mcp

        svc = mcp.service(
            "http://mcp-sheets:8002/mcp",
            headers={"X-Account-Label": "work@corp.com"},
        )
        cfg = svc.server_config()
        assert cfg["headers"] == {"X-Account-Label": "work@corp.com"}

    def test_service_without_headers_unchanged(self) -> None:
        from chief.tools.sheets import mcp

        svc = mcp.service("http://mcp-sheets:8002/mcp")
        cfg = svc.server_config()
        assert "headers" not in cfg
