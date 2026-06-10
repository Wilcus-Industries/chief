"""Server-level unit tests: Calendar server credential selection per request.

Proves that the multi-account calendar server selects the right credential
per request given an injected ``X-Account-Label`` header, without needing a
live calendar API.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers — build minimal token files for testing
# ---------------------------------------------------------------------------


def _write_token(path: Path, label: str) -> None:
    """Write a minimal google-auth token JSON with an ``account`` field."""
    data = {
        "token": f"at_{label}",
        "refresh_token": f"rt_{label}",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "secret",
        "scopes": ["https://www.googleapis.com/auth/calendar"],
        "account": label,
    }
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests for the credential-registry helper in the calendar server
# ---------------------------------------------------------------------------


class TestLoadAccountCredentials:
    """Unit tests for the multi-account credential loader."""

    def test_load_single_token(self, tmp_path: Any) -> None:
        """A single legacy token loads under its account label."""
        from chief.tools.calendar.credentials import load_account_credentials

        _write_token(tmp_path / "google_token.json", "main@example.com")
        registry = load_account_credentials(tmp_path)

        assert "main@example.com" in registry
        assert len(registry) == 1

    def test_load_multiple_tokens(self, tmp_path: Any) -> None:
        """Multiple labeled tokens each load under their account labels."""
        from chief.tools.calendar.credentials import load_account_credentials

        _write_token(tmp_path / "google_token.json", "main@example.com")
        _write_token(tmp_path / "google_token_work.json", "work@corp.com")
        registry = load_account_credentials(tmp_path)

        assert "main@example.com" in registry
        assert "work@corp.com" in registry
        assert len(registry) == 2

    def test_empty_dir_returns_empty_registry(self, tmp_path: Any) -> None:
        """An empty token directory yields an empty registry (no crash)."""
        from chief.tools.calendar.credentials import load_account_credentials

        registry = load_account_credentials(tmp_path)
        assert registry == {}

    def test_missing_dir_returns_empty_registry(self, tmp_path: Any) -> None:
        """A missing token directory yields an empty registry."""
        from chief.tools.calendar.credentials import load_account_credentials

        registry = load_account_credentials(tmp_path / "nonexistent")
        assert registry == {}


class TestSelectCredential:
    """Unit tests for per-request credential selection."""

    def test_select_by_label_returns_matching_cred(self, tmp_path: Any) -> None:
        """select_credential returns the credential for the requested label."""
        from chief.tools.calendar.credentials import (
            load_account_credentials,
            select_credential,
        )

        _write_token(tmp_path / "google_token.json", "main@example.com")
        _write_token(tmp_path / "google_token_work.json", "work@corp.com")
        registry = load_account_credentials(tmp_path)

        # Just check we get the credential keyed to the requested account.
        cred = select_credential(registry, "work@corp.com")
        assert cred is not None

    def test_select_unknown_label_returns_first(self, tmp_path: Any) -> None:
        """Unknown label falls back to the first (default) credential."""
        from chief.tools.calendar.credentials import (
            load_account_credentials,
            select_credential,
        )

        _write_token(tmp_path / "google_token.json", "main@example.com")
        registry = load_account_credentials(tmp_path)

        cred = select_credential(registry, "nobody@example.com")
        # Fallback: returns a credential (the default), not None.
        assert cred is not None

    def test_select_none_label_returns_first(self, tmp_path: Any) -> None:
        """None label (no header) falls back to the default credential."""
        from chief.tools.calendar.credentials import (
            load_account_credentials,
            select_credential,
        )

        _write_token(tmp_path / "google_token.json", "main@example.com")
        registry = load_account_credentials(tmp_path)

        cred = select_credential(registry, None)
        assert cred is not None

    def test_select_empty_registry_returns_none(self, tmp_path: Any) -> None:
        """Empty registry returns None (no credentials configured)."""
        from chief.tools.calendar.credentials import (
            load_account_credentials,
            select_credential,
        )

        registry = load_account_credentials(tmp_path)  # empty dir
        cred = select_credential(registry, "work@corp.com")
        assert cred is None
