"""Runtime add-account via chat consent (issue #53).

The OAuth code-exchange and email lookup are injected, so these run without a browser
or a real Google account: they exercise consent-URL generation, code extraction, the
exchange-+-store flow, and the owner-only ``add_account`` MCP tool's two-step
behaviour. The live consent dance is a manual verification.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from chief.tools.google import auth
from chief.tools.google.accounts import discover_accounts, token_path_for
from chief.tools.google.add_account_service import AddAccountService

_CLIENT_JSON = {
    "installed": {
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


def _write_client(tmp_path: Path) -> Path:
    client = tmp_path / "google_oauth_client.json"
    client.write_text(json.dumps(_CLIENT_JSON), encoding="utf-8")
    return client


class _FakeCredentials:
    """Stand-in for exchanged google-auth credentials — only ``to_json``."""

    def __init__(self, refresh_token: str = "rt") -> None:
        self._refresh_token = refresh_token

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": "at",
                "refresh_token": self._refresh_token,
                "token_uri": auth._TOKEN_URI,
                "client_id": "cid",
                "client_secret": "secret",
                "scopes": list(auth.SCOPES),
            }
        )


# --- consent URL + code extraction -------------------------------------------------


def test_build_consent_url_has_client_scopes_and_offline(tmp_path: Path) -> None:
    url = auth.build_consent_url(client_secrets=_write_client(tmp_path))
    assert url.startswith(auth._AUTH_URI + "?")
    assert "client_id=cid.apps.googleusercontent.com" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    # All four scopes ride the URL (url-encoded, space-joined).
    for scope in auth.SCOPES:
        assert scope.replace(":", "%3A").replace("/", "%2F") in url


def test_build_consent_url_missing_client_raises(tmp_path: Path) -> None:
    with pytest.raises(auth.ClientSecretsMissing):
        auth.build_consent_url(client_secrets=tmp_path / "absent.json")


def test_extract_auth_code_accepts_bare_code() -> None:
    assert auth.extract_auth_code("  4/abc-DEF ") == "4/abc-DEF"


def test_extract_auth_code_pulls_from_redirect_url() -> None:
    pasted = "http://localhost/?code=4/abc-DEF&scope=email%20profile"
    assert auth.extract_auth_code(pasted) == "4/abc-DEF"


# --- exchange + store --------------------------------------------------------------


def test_add_account_from_code_stores_token_labeled_by_email(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def fake_exchange(code: str) -> auth._Credentials:
        captured["code"] = code
        return _FakeCredentials()

    out, email = auth.add_account_from_code(
        pasted="http://localhost/?code=THE_CODE&scope=x",
        secrets_dir=tmp_path,
        client_secrets=_write_client(tmp_path),
        exchange=fake_exchange,
        email_from_credentials=lambda _c: "work@corp.com",
    )

    assert captured["code"] == "THE_CODE"  # redirect URL was parsed to the bare code
    assert email == "work@corp.com"
    assert out == token_path_for(tmp_path, "work@corp.com")
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["refresh_token"] == "rt"
    # The email is stamped into the account field so the registry labels it w/o a call.
    assert written["account"] == "work@corp.com"


def test_added_account_is_discoverable(tmp_path: Path) -> None:
    auth.add_account_from_code(
        pasted="THE_CODE",
        secrets_dir=tmp_path,
        client_secrets=_write_client(tmp_path),
        exchange=lambda _c: _FakeCredentials(),
        email_from_credentials=lambda _c: "work@corp.com",
    )
    accounts = discover_accounts(tmp_path)
    assert [a.label for a in accounts] == ["work@corp.com"]
    assert accounts[0].email == "work@corp.com"


def test_token_path_for_never_clobbers_legacy(tmp_path: Path) -> None:
    path = token_path_for(tmp_path, "any@x.com")
    assert path.name != "google_token.json"
    assert path.name.startswith("google_token_")


# --- the owner-only add_account MCP tool (two-step) --------------------------------


def _service(tmp_path: Path, *, refresh_token: str = "rt") -> AddAccountService:
    return AddAccountService(
        secrets_dir=tmp_path,
        client_secrets=_write_client(tmp_path),
        exchange=lambda _c: _FakeCredentials(refresh_token),
        email_from_credentials=lambda _c: "work@corp.com",
    )


async def _call(
    service: AddAccountService, args: dict[str, object]
) -> dict[str, Any]:
    tool = service._build_tool()
    return await tool.handler(args)


@pytest.mark.asyncio
async def test_add_account_tool_step1_returns_consent_url(tmp_path: Path) -> None:
    result = await _call(_service(tmp_path), {})
    assert result["is_error"] is False
    text = result["content"][0]["text"]
    assert auth._AUTH_URI in text


@pytest.mark.asyncio
async def test_add_account_tool_step2_stores_and_confirms(tmp_path: Path) -> None:
    result = await _call(_service(tmp_path), {"code": "http://localhost/?code=X"})
    assert result["is_error"] is False
    assert "work@corp.com" in result["content"][0]["text"]
    # The token landed and is discoverable — no restart.
    assert [a.label for a in discover_accounts(tmp_path)] == ["work@corp.com"]


@pytest.mark.asyncio
async def test_add_account_tool_reports_exchange_failure(tmp_path: Path) -> None:
    def boom(_c: str) -> auth._Credentials:
        raise RuntimeError("bad code")

    service = AddAccountService(
        secrets_dir=tmp_path,
        client_secrets=_write_client(tmp_path),
        exchange=boom,
        email_from_credentials=lambda _c: "work@corp.com",
    )
    result = await _call(service, {"code": "X"})
    assert result["is_error"] is True
    assert "bad code" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_add_account_tool_no_client_reports_error(tmp_path: Path) -> None:
    service = AddAccountService(
        secrets_dir=tmp_path,
        client_secrets=tmp_path / "absent.json",
    )
    result = await _call(service, {})
    assert result["is_error"] is True
    assert "OAuth client" in result["content"][0]["text"]
