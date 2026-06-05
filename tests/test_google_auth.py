"""The shared Google OAuth consent helper (``chief.tools.google.auth``).

The interactive loopback consent is injected, so these run without a browser: they
exercise path handling, scope wiring, the google-auth token write, and the CLI's exit
codes / operator instructions.
"""

import json
from pathlib import Path

import pytest

from chief.tools.google import auth


class _FakeCredentials:
    """Stand-in for ``google.oauth2.credentials.Credentials`` — only ``to_json``."""

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": "at",
                "refresh_token": "rt",
                "scopes": list(auth.SCOPES),
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "cid",
                "client_secret": "secret",
            }
        )


def _fake_consent(captured: dict[str, object]) -> auth.ConsentFn:
    """A consent fn that records its args and returns canned credentials."""

    def consent(client_secrets: Path, scopes: list[str]) -> auth._Credentials:
        captured["client"] = client_secrets
        captured["scopes"] = scopes
        return _FakeCredentials()

    return consent


def test_scopes_cover_calendar_drive_sheets_gmail() -> None:
    joined = " ".join(auth.SCOPES)
    assert "auth/calendar" in joined
    assert "auth/drive" in joined
    assert "auth/spreadsheets" in joined
    assert "auth/gmail.modify" in joined


def test_mint_token_writes_google_auth_token(tmp_path: Path) -> None:
    client = tmp_path / "client.json"
    client.write_text("{}", encoding="utf-8")
    out = tmp_path / "nested" / "token.json"  # parent must be created
    captured: dict[str, object] = {}

    result = auth.mint_token(
        client_secrets=client, token_out=out, consent=_fake_consent(captured)
    )

    assert result == out
    written = json.loads(out.read_text(encoding="utf-8"))
    # Native google-auth shape — refresh_token is the durable bit every server reads.
    assert written["refresh_token"] == "rt"
    assert written["token"] == "at"
    assert written["scopes"] == list(auth.SCOPES)
    assert captured["client"] == client
    assert captured["scopes"] == list(auth.SCOPES)


def test_mint_token_missing_client_raises(tmp_path: Path) -> None:
    with pytest.raises(auth.ClientSecretsMissing):
        auth.mint_token(
            client_secrets=tmp_path / "absent.json",
            token_out=tmp_path / "token.json",
            consent=_fake_consent({}),
        )


def test_main_reports_missing_client(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = auth.main(
        ["--client", str(tmp_path / "absent.json"), "--out", str(tmp_path / "t.json")]
    )

    assert code == 1
    assert "not found" in capsys.readouterr().out.lower()


def test_main_success_writes_and_instructs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = tmp_path / "client.json"
    client.write_text("{}", encoding="utf-8")
    out = tmp_path / "token.json"
    monkeypatch.setattr(auth, "_run_local_consent", _fake_consent({}))

    code = auth.main(["--client", str(client), "--out", str(out)])

    assert code == 0
    assert out.exists()
    assert "mcp-calendar" in capsys.readouterr().out
