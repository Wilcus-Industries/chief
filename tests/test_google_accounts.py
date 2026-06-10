"""Google account registry + list_accounts tool (issue #44).

Tests cover:
- discover_accounts scans a secrets dir for google_token*.json files
- backward-compat: legacy google_token.json registers as first account labeled by email
- additional google_token_<label>.json files each register as separate accounts
- graceful degradation when account field is absent (unknown email)
- mint_token extended to write the account (email) field via an injected email_fn
- ListAccountsService: owner-only in-process tool returns formatted account list
- guest sessions have no access to list_accounts (tier isolation by construction)
- build_list_accounts_service() defaults to /token (the compose bind-mount, issue #54)
- backward-compat email resolution: tokens without account field resolve email via
  injected resolver (issue #54)
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

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

    def test_build_list_accounts_service_default_path_is_production_mount(
        self,
    ) -> None:
        """Default secrets_dir must be /token — the compose bind-mount for core.

        docker-compose.yml mounts ./secrets/google_token.json into core at
        /token/google_token.json.  Using the cwd-relative ``secrets/`` would
        scan an empty/absent directory and always return zero accounts.
        """
        import inspect

        from chief.app import build_list_accounts_service

        sig = inspect.signature(build_list_accounts_service)
        default = sig.parameters["secrets_dir"].default
        # The production default must be Path("/token"), matching the compose mount.
        assert default == Path("/token"), (
            f"build_list_accounts_service default secrets_dir is {default!r}; "
            "expected Path('/token') (the compose bind-mount for core). "
            "A cwd-relative 'secrets/' scans an empty dir and always returns zero."
        )


# ---------------------------------------------------------------------------
# Backward-compat email resolution (issue #54): tokens without 'account' field
# ---------------------------------------------------------------------------


class TestBackwardCompatEmailResolution:
    """Legacy tokens (no 'account' field) must resolve email via the token credentials.

    discover_accounts accepts an optional ``email_resolver`` callable so tests
    can inject a fake; the production default uses a real OAuth token exchange.
    """

    def test_legacy_token_without_account_resolves_email_via_resolver(
        self, tmp_path: Path
    ) -> None:
        """When a token has no 'account' field, the injected resolver is called."""
        # Write a legacy token without account field
        data = {
            "token": "at",
            "refresh_token": "rt",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "cid",
            "client_secret": "secret",
        }
        (tmp_path / "google_token.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

        resolved_emails: list[Path] = []

        def fake_resolver(token_path: Path) -> str | None:
            resolved_emails.append(token_path)
            return "legacy@example.com"

        accounts = discover_accounts(tmp_path, email_resolver=fake_resolver)

        assert len(accounts) == 1
        assert accounts[0].email == "legacy@example.com"
        assert accounts[0].label == "legacy@example.com"
        assert len(resolved_emails) == 1

    def test_token_with_account_field_skips_resolver(self, tmp_path: Path) -> None:
        """Tokens that already have 'account' don't call the resolver."""
        _write_token(tmp_path / "google_token.json", email="known@example.com")

        resolver_called = False

        def fake_resolver(token_path: Path) -> str | None:
            nonlocal resolver_called
            resolver_called = True
            return "should-not-be-called@example.com"

        accounts = discover_accounts(tmp_path, email_resolver=fake_resolver)

        assert accounts[0].email == "known@example.com"
        assert not resolver_called

    def test_resolver_returning_none_falls_back_to_slug(
        self, tmp_path: Path
    ) -> None:
        """If the resolver can't determine the email, fall back to the filename slug."""
        data = {
            "token": "at",
            "refresh_token": "rt",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "cid",
            "client_secret": "secret",
        }
        (tmp_path / "google_token.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

        accounts = discover_accounts(
            tmp_path, email_resolver=lambda _path: None
        )

        assert accounts[0].email is None
        assert accounts[0].label == "google_token"

    def test_discover_accounts_no_resolver_still_works(
        self, tmp_path: Path
    ) -> None:
        """discover_accounts without email_resolver behaves as before for legacy."""
        _write_token(tmp_path / "google_token.json", email=None)

        accounts = discover_accounts(tmp_path)

        assert len(accounts) == 1
        assert accounts[0].email is None
        assert accounts[0].label == "google_token"


# ---------------------------------------------------------------------------
# auth.main() wires email_fn in production (issue #54)
# ---------------------------------------------------------------------------


class TestAuthMainWiresEmailFn:
    """Production CLI must wire an email_fn so freshly minted tokens carry 'account'."""

    def test_main_wires_email_fn_and_token_has_account_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After production mint, the token JSON must contain the 'account' field."""
        from chief.tools.google import auth

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

        monkeypatch.setattr(auth, "_run_local_consent", lambda _c, _s: _FakeCreds())
        # Patch the userinfo fetch so it doesn't need network
        with patch(
            "chief.tools.google.auth._fetch_userinfo_email",
            return_value="prod@example.com",
        ):
            code = auth.main(["--client", str(client), "--out", str(out)])

        assert code == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data.get("account") == "prod@example.com"

    def test_main_without_userinfo_still_succeeds_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If userinfo fetch fails, mint still completes (no account field, no crash).

        Graceful degradation: the token is written without the 'account' field.
        """
        from chief.tools.google import auth

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

        monkeypatch.setattr(auth, "_run_local_consent", lambda _c, _s: _FakeCreds())
        # Patch userinfo to raise, simulating a network failure
        with patch(
            "chief.tools.google.auth._fetch_userinfo_email",
            side_effect=Exception("network error"),
        ):
            code = auth.main(["--client", str(client), "--out", str(out)])

        # Mint still succeeds; account field is absent (graceful degradation)
        assert code == 0
        assert out.exists()


# ---------------------------------------------------------------------------
# Production wiring: build_list_accounts_service passes a real email_resolver
# ---------------------------------------------------------------------------


class TestProductionResolverWiring:
    """build_list_accounts_service must pass a real email_resolver to discover_accounts.

    The resolver is read-only: it refreshes the token in-memory and calls the
    Google userinfo endpoint — it never writes the token back to disk.
    """

    @pytest.mark.asyncio
    async def test_production_path_resolves_legacy_token_email(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy token (no 'account' field) must label by email through production.

        With dynamic re-scan (issue #50), discovery happens at tool-call time so we
        invoke the tool handler to verify the resolver is used correctly.
        """
        import json as _json

        import chief.app as app_module
        from chief.app import build_list_accounts_service

        # Write a legacy token with no 'account' field
        legacy_data = {
            "token": None,
            "refresh_token": "rt-legacy",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "cid",
            "client_secret": "secret",
            "scopes": ["https://www.googleapis.com/auth/calendar"],
        }
        (tmp_path / "google_token.json").write_text(
            _json.dumps(legacy_data), encoding="utf-8"
        )

        # Patch the resolver used internally so no network call happens.
        monkeypatch.setattr(
            app_module,
            "_resolve_email_from_token",
            lambda token_path: "legacy@example.com",
        )

        svc = build_list_accounts_service(secrets_dir=tmp_path)
        # Dynamic mode: discovery happens at tool-call time.
        tool_obj = svc._build_tool()
        result = await tool_obj.handler({})
        text = result["content"][0]["text"]
        assert "legacy@example.com" in text, (
            f"Expected resolved email in list_accounts output; got: {text!r}"
        )

    def test_production_resolver_is_passed_not_none(
        self, tmp_path: Path
    ) -> None:
        """build_list_accounts_service must wire a non-None email_resolver.

        With dynamic re-scan (issue #50) the resolver is stored on the service and
        passed to discover_accounts at call time — we verify the field is set.
        """
        from chief.app import build_list_accounts_service

        svc = build_list_accounts_service(secrets_dir=tmp_path)
        assert svc.email_resolver is not None, (
            "build_list_accounts_service email_resolver is None; "
            "legacy tokens will never resolve their email"
        )

    @pytest.mark.asyncio
    async def test_production_resolver_degrades_gracefully_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the resolver raises, list_accounts falls back to the slug label.

        With dynamic re-scan (issue #50), discovery happens at tool-call time.
        """
        import json as _json

        import chief.app as app_module
        from chief.app import build_list_accounts_service

        legacy_data = {
            "token": None,
            "refresh_token": "rt",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "cid",
            "client_secret": "secret",
        }
        (tmp_path / "google_token.json").write_text(
            _json.dumps(legacy_data), encoding="utf-8"
        )

        def _failing_resolver(token_path: object) -> str | None:
            raise RuntimeError("network unavailable")

        monkeypatch.setattr(app_module, "_resolve_email_from_token", _failing_resolver)

        # Must not raise — graceful degradation to slug
        svc = build_list_accounts_service(secrets_dir=tmp_path)
        tool_obj = svc._build_tool()
        result = await tool_obj.handler({})
        # Resolver raised -> _to_account caught it -> falls back to slug "google_token"
        text = result["content"][0]["text"]
        assert "google_token" in text, (
            f"Expected slug label 'google_token' in fallback output; got: {text!r}"
        )
        assert result["is_error"] is False
