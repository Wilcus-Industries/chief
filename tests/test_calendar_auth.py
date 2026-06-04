"""The Google Calendar OAuth consent helper (``chief.tools.calendar.auth``).

The interactive loopback consent is injected, so these run without a browser: they
exercise path handling, scope wiring, the nspady Node token write, and the CLI's exit
codes / operator instructions.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chief.tools.calendar import auth

_EXPIRY = datetime(2026, 1, 1, 12, 0, 0)  # naive UTC, like google-auth's Credentials


class _FakeCredentials:
    """Stand-in for ``google.oauth2.credentials.Credentials`` (read attributes).

    Attributes are annotated to the optional types the ``_Credentials`` protocol
    declares — protocol attributes are invariant, so a bare ``str`` wouldn't satisfy it.
    """

    def __init__(self) -> None:
        self.token: str | None = "at"
        self.refresh_token: str | None = "rt"
        self.scopes: Sequence[str] | None = list(auth.SCOPES)
        self.expiry: datetime | None = _EXPIRY


def _fake_consent(captured: dict[str, object]) -> auth.ConsentFn:
    """A consent fn that records its args and returns canned credentials."""

    def consent(client_secrets: Path, scopes: list[str]) -> auth._Credentials:
        captured["client"] = client_secrets
        captured["scopes"] = scopes
        return _FakeCredentials()

    return consent


def test_mint_token_writes_nspady_node_token(tmp_path: Path) -> None:
    client = tmp_path / "client.json"
    client.write_text("{}", encoding="utf-8")
    out = tmp_path / "nested" / "token.json"  # parent must be created
    captured: dict[str, object] = {}

    result = auth.mint_token(
        client_secrets=client, token_out=out, consent=_fake_consent(captured)
    )

    assert result == out
    written = json.loads(out.read_text(encoding="utf-8"))
    # Pre-wrapped under the 'normal' account so nspady loads it without a migration
    # write (it would otherwise rewrite a single-account file on first boot).
    account = written["normal"]
    assert account["refresh_token"] == "rt"
    assert account["access_token"] == "at"
    assert account["token_type"] == "Bearer"
    # scopes (list) → scope (space-delimited string), the Node google-auth shape.
    assert account["scope"] == auth.SCOPES[0]
    expected_ms = int(_EXPIRY.replace(tzinfo=UTC).timestamp() * 1000)
    assert account["expiry_date"] == expected_ms
    assert captured["client"] == client
    assert captured["scopes"] == list(auth.SCOPES)


def test_to_node_token_omits_expiry_when_absent() -> None:
    creds = _FakeCredentials()
    creds.expiry = None

    account = auth._to_node_token(creds)["normal"]

    # No expiry_date key → nspady refreshes on first use via the refresh_token.
    assert "expiry_date" not in account
    assert account["refresh_token"] == "rt"


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
    assert "mcp-gcal" in capsys.readouterr().out
