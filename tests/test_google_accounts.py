"""Google account registry + list_accounts tool (issue #44).

Tests cover:
- discover_accounts scans a secrets dir for google_token*.json files
- backward-compat: legacy google_token.json registers as first account labeled by email
- additional google_token_<label>.json files each register as separate accounts
- graceful degradation when account field is absent (unknown email)
- mint_token extended to write the account (email) field via an injected email_fn
- ListAccountsService: owner-only in-process tool returns formatted account list
- guest sessions have no access to list_accounts (tier isolation by construction)
"""

import json
from pathlib import Path
from typing import Any

import pytest

from chief.tools.google.accounts import GoogleAccount, discover_accounts
from chief.tools.google.auth import mint_token

# ---------------------------------------------------------------------------
# Account registry — discover_accounts
# ---------------------------------------------------------------------------


def _write_token(path: Path, email: str | None, label: str | None = None) -> None:
    """Write a minimal google-auth token JSON to path, with optional account/label."""
    data: dict[str, Any] = {
        "token": "at",
        "refresh_token": "rt",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "secret",
    }
    if email is not None:
        data["account"] = email
    path.write_text(json.dumps(data), encoding="utf-8")


class TestDiscoverAccounts:
    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert discover_accounts(tmp_path) == []

    def test_single_legacy_token_registers_one_account(self, tmp_path: Path) -> None:
        _write_token(tmp_path / "google_token.json", email="owner@example.com")

        accounts = discover_accounts(tmp_path)

        assert len(accounts) == 1
        assert accounts[0].label == "owner@example.com"
        assert accounts[0].email == "owner@example.com"

    def test_second_token_file_registers_second_account(self, tmp_path: Path) -> None:
        _write_token(tmp_path / "google_token.json", email="a@example.com")
        _write_token(tmp_path / "google_token_work.json", email="b@work.example.com")

        accounts = discover_accounts(tmp_path)

        assert len(accounts) == 2
        emails = {a.email for a in accounts}
        assert emails == {"a@example.com", "b@work.example.com"}

    def test_legacy_token_is_first_in_list(self, tmp_path: Path) -> None:
        _write_token(tmp_path / "google_token.json", email="first@example.com")
        _write_token(tmp_path / "google_token_second.json", email="second@example.com")

        accounts = discover_accounts(tmp_path)

        assert accounts[0].email == "first@example.com"

    def test_label_token_uses_email_as_label_when_present(self, tmp_path: Path) -> None:
        _write_token(tmp_path / "google_token_work.json", email="work@example.com")

        accounts = discover_accounts(tmp_path)

        # label == email (the account's Google email is its canonical label)
        assert accounts[0].label == "work@example.com"
        assert accounts[0].email == "work@example.com"

    def test_no_email_field_falls_back_gracefully(self, tmp_path: Path) -> None:
        """Existing tokens without account field degrade to label-only, not a crash."""
        _write_token(tmp_path / "google_token.json", email=None)

        accounts = discover_accounts(tmp_path)

        assert len(accounts) == 1
        # label falls back to filename slug; email is None
        assert accounts[0].label == "google_token"
        assert accounts[0].email is None

    def test_non_json_files_are_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "google_token.txt").write_text("not a token")
        token_data = json.dumps({"token": "x", "account": "ok@example.com"})
        (tmp_path / "google_token.json").write_text(token_data)

        accounts = discover_accounts(tmp_path)

        assert len(accounts) == 1
        assert accounts[0].email == "ok@example.com"

    def test_nonexistent_dir_returns_empty(self, tmp_path: Path) -> None:
        absent = tmp_path / "nowhere"
        assert discover_accounts(absent) == []

    def test_multiple_label_tokens_sorted_by_filename(self, tmp_path: Path) -> None:
        _write_token(tmp_path / "google_token_z.json", email="z@example.com")
        _write_token(tmp_path / "google_token_a.json", email="a@example.com")

        accounts = discover_accounts(tmp_path)

        # Sorted by filename (a < z), so a@... is first
        emails = [a.email for a in accounts]
        assert emails == ["a@example.com", "z@example.com"]


# ---------------------------------------------------------------------------
# mint_token extended: email_fn injects the account field
# ---------------------------------------------------------------------------


class TestMintTokenEmailField:
    def test_mint_token_writes_account_field_when_email_fn_provided(
        self, tmp_path: Path
    ) -> None:
        client = tmp_path / "client.json"
        client.write_text("{}", encoding="utf-8")
        out = tmp_path / "token.json"

        class _FakeCreds:
            def to_json(self) -> str:
                return json.dumps(
                    {
                        "token": "at",
                        "refresh_token": "rt",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "client_id": "cid",
                        "client_secret": "secret",
                    }
                )

        captured: dict[str, object] = {}

        def _consent(_client: Path, _scopes: list[str]) -> "_FakeCreds":
            captured["called"] = True
            return _FakeCreds()

        def _email_fn() -> str:
            return "minted@example.com"

        mint_token(
            client_secrets=client,
            token_out=out,
            consent=_consent,
            email_fn=_email_fn,
        )

        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["account"] == "minted@example.com"

    def test_mint_token_without_email_fn_omits_account_field(
        self, tmp_path: Path
    ) -> None:
        client = tmp_path / "client.json"
        client.write_text("{}", encoding="utf-8")
        out = tmp_path / "token.json"

        class _FakeCreds:
            def to_json(self) -> str:
                return json.dumps(
                    {
                        "token": "at",
                        "refresh_token": "rt",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "client_id": "cid",
                        "client_secret": "secret",
                    }
                )

        def _consent(_client: Path, _scopes: list[str]) -> "_FakeCreds":
            return _FakeCreds()

        mint_token(
            client_secrets=client,
            token_out=out,
            consent=_consent,
        )

        data = json.loads(out.read_text(encoding="utf-8"))
        # no account field — this is the backward-compat legacy path
        assert "account" not in data


# ---------------------------------------------------------------------------
# ListAccountsService — owner-only in-process MCP tool
# ---------------------------------------------------------------------------


class TestListAccountsService:
    def _make_service(self, accounts: list[GoogleAccount]) -> Any:
        from chief.tools.google.list_accounts_service import ListAccountsService

        return ListAccountsService(accounts=accounts)

    def test_server_config_returns_sdk_mcp_server(self) -> None:
        svc = self._make_service([])
        cfg = svc.server_config()
        # SDK MCP server config has 'type': 'sdk'
        assert cfg.get("type") == "sdk"

    def test_tool_name_is_qualified(self) -> None:
        svc = self._make_service([])
        assert svc.tool_name == "mcp__chief_accounts__list_accounts"

    @pytest.mark.asyncio
    async def test_list_accounts_returns_formatted_list(self) -> None:
        accounts = [
            GoogleAccount(label="personal@example.com", email="personal@example.com"),
            GoogleAccount(label="work@corp.com", email="work@corp.com"),
        ]
        svc = self._make_service(accounts)
        # Build the tool directly (bypassing the SDK server plumbing)
        tool_obj = svc._build_tool()
        result = await tool_obj.handler({})

        text = result["content"][0]["text"]
        assert "personal@example.com" in text
        assert "work@corp.com" in text

    @pytest.mark.asyncio
    async def test_list_accounts_empty(self) -> None:
        svc = self._make_service([])
        tool_obj = svc._build_tool()
        result = await tool_obj.handler({})

        text = result["content"][0]["text"]
        assert "no google accounts" in text.lower()


# ---------------------------------------------------------------------------
# Wiring in app.py: build_accounts_service uses settings secrets_dir
# ---------------------------------------------------------------------------


class TestBuildAccountsService:
    def test_build_accounts_service_discovers_from_secrets_dir(
        self, tmp_path: Path
    ) -> None:
        _write_token(tmp_path / "google_token.json", email="main@example.com")
        _write_token(tmp_path / "google_token_work.json", email="work@example.com")

        from chief.app import build_list_accounts_service

        svc = build_list_accounts_service(secrets_dir=tmp_path)
        assert svc is not None
        tool_name = svc.tool_name
        assert tool_name == "mcp__chief_accounts__list_accounts"

    def test_build_accounts_service_absent_dir_returns_service_with_no_accounts(
        self, tmp_path: Path
    ) -> None:
        from chief.app import build_list_accounts_service

        absent = tmp_path / "nowhere"
        svc = build_list_accounts_service(secrets_dir=absent)
        # still returns a service (owner can call it; it just reports 0 accounts)
        assert svc is not None
