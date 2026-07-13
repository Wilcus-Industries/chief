"""Web settings + health tests (#153): curated forms through the real app.

Real HTTP against the real ASGI app; the write targets are a real tmp secrets dir
and a real config.yaml. The ONLY fake is the token validator — the third-party API
boundary — injected through the SettingsPanel seam.
"""

import stat
from pathlib import Path

import httpx
import pytest

from chief.config import Settings
from chief.web.app import WebDeps, build_web_app
from chief.web.auth import SESSION_COOKIE, WebAuth
from chief.web.health import build_health_checks
from chief.web.settings_io import OwnerConfig, SecretsStore, SettingsPanel

PASSWORD = "settingspassword"

_INITIAL_CONFIG = """\
# owner ids — a load-bearing comment that must survive edits
owner_telegram_id: 0
owner_discord_id: 0
owner_model_default: claude-sonnet-4-6
web_lan_enabled: false
"""


class RecordingValidator:
    """A validator seam that records calls and returns a scripted verdict."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.seen: list[str] = []

    async def __call__(self, token: str) -> str | None:
        self.seen.append(token)
        return self.error


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(_INITIAL_CONFIG)
    return path


@pytest.fixture
def panel(tmp_path: Path, config_path: Path) -> SettingsPanel:
    return SettingsPanel(
        secrets=SecretsStore(tmp_path / "secrets"),
        config=OwnerConfig(config_path),
        settings=Settings(),
        validators={
            "telegram": RecordingValidator(),
            "discord": RecordingValidator(),
            "openrouter": RecordingValidator(),
        },
    )


@pytest.fixture
def auth(tmp_path: Path) -> WebAuth:
    a = WebAuth(tmp_path / "secrets")
    a.set_password(PASSWORD)
    return a


@pytest.fixture
async def client(auth: WebAuth, panel: SettingsPanel) -> httpx.AsyncClient:
    app = build_web_app(
        WebDeps(
            auth=auth,
            settings_panel=panel,
            health=build_health_checks(Settings()),
        )
    )
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://web"
    )
    resp = await http.post("/login", data={"password": PASSWORD})
    assert resp.status_code == 303
    return http


async def test_connect_telegram_validates_writes_secret_and_owner_id(
    client: httpx.AsyncClient, panel: SettingsPanel, tmp_path: Path, config_path: Path
) -> None:
    resp = await client.post(
        "/settings/platform/telegram",
        data={"token": "123:abc", "owner_id": "42"},
    )
    assert resp.status_code == 303

    validator = panel.validators["telegram"]
    assert isinstance(validator, RecordingValidator) and validator.seen == ["123:abc"]

    token_file = tmp_path / "secrets" / "telegram_bot_token"
    assert token_file.read_text() == "123:abc"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    text = config_path.read_text()
    assert "owner_telegram_id: 42" in text
    assert "load-bearing comment" in text  # comments survive the edit
    assert "owner_discord_id: 0" in text  # untouched keys stay put


async def test_invalid_token_writes_nothing(
    client: httpx.AsyncClient, panel: SettingsPanel, tmp_path: Path, config_path: Path
) -> None:
    validator = panel.validators["telegram"]
    assert isinstance(validator, RecordingValidator)
    validator.error = "Telegram rejected the credential (HTTP 401)"

    resp = await client.post(
        "/settings/platform/telegram",
        data={"token": "bad", "owner_id": "42"},
    )
    assert resp.status_code == 400
    assert "rejected" in resp.text
    assert not (tmp_path / "secrets" / "telegram_bot_token").exists()
    assert "owner_telegram_id: 0" in config_path.read_text()


async def test_non_numeric_owner_id_is_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/settings/platform/telegram",
        data={"token": "123:abc", "owner_id": "bob"},
    )
    assert resp.status_code == 400


async def test_unknown_platform_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/settings/platform/matrix", data={"token": "x", "owner_id": "1"}
    )
    assert resp.status_code == 404


async def test_disconnect_removes_token_and_zeroes_owner_id(
    client: httpx.AsyncClient, tmp_path: Path, config_path: Path
) -> None:
    await client.post(
        "/settings/platform/telegram",
        data={"token": "123:abc", "owner_id": "42"},
    )
    resp = await client.post(
        "/settings/platform/telegram", data={"action": "disconnect"}
    )
    assert resp.status_code == 303
    assert not (tmp_path / "secrets" / "telegram_bot_token").exists()
    assert "owner_telegram_id: 0" in config_path.read_text()


async def test_openrouter_key_connect_and_disconnect(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    resp = await client.post("/settings/openrouter", data={"api_key": "sk-or-123"})
    assert resp.status_code == 303
    key_file = tmp_path / "secrets" / "openrouter_api_key"
    assert key_file.read_text() == "sk-or-123"

    resp = await client.post("/settings/openrouter", data={"action": "disconnect"})
    assert resp.status_code == 303
    assert not key_file.exists()


async def test_lan_toggle_writes_the_flag(
    client: httpx.AsyncClient, config_path: Path
) -> None:
    resp = await client.post("/settings/web", data={"lan": "on"})
    assert resp.status_code == 303
    assert "web_lan_enabled: true" in config_path.read_text()

    resp = await client.post("/settings/web", data={})
    assert resp.status_code == 303
    assert "web_lan_enabled: false" in config_path.read_text()


async def test_quiet_hours_written_and_validated(
    client: httpx.AsyncClient, config_path: Path
) -> None:
    import yaml

    resp = await client.post(
        "/settings/quiet-hours", data={"start": "22:00", "end": "07:00"}
    )
    assert resp.status_code == 303
    loaded = yaml.safe_load(config_path.read_text())
    # Round-trips as STRINGS — the config validator would reject an int here
    # ("22:00" is a YAML 1.1 sexagesimal, so correct quoting is load-bearing).
    assert loaded["quiet_hours_start"] == "22:00"
    assert loaded["quiet_hours_end"] == "07:00"

    resp = await client.post(
        "/settings/quiet-hours", data={"start": "9am", "end": "07:00"}
    )
    assert resp.status_code == 400
    loaded = yaml.safe_load(config_path.read_text())
    assert loaded["quiet_hours_start"] == "22:00"  # untouched


async def test_model_selection_written(
    client: httpx.AsyncClient, config_path: Path
) -> None:
    resp = await client.post(
        "/settings/model", data={"owner_model_default": "claude-opus-4-8"}
    )
    assert resp.status_code == 303
    assert "owner_model_default: claude-opus-4-8" in config_path.read_text()


async def test_password_change_revokes_other_sessions(
    client: httpx.AsyncClient, auth: WebAuth
) -> None:
    stale = auth.issue_session()  # another browser's session

    resp = await client.post(
        "/settings/password",
        data={
            "current": PASSWORD,
            "password": "a-new-password",
            "confirm": "a-new-password",
        },
    )
    assert resp.status_code == 303
    assert not auth.session_valid(stale)
    assert auth.verify("a-new-password")
    # The changing browser got a fresh session cookie and stays logged in.
    assert client.cookies.get(SESSION_COOKIE)
    page = await client.get("/settings")
    assert page.status_code == 200


async def test_password_change_requires_the_current_password(
    client: httpx.AsyncClient, auth: WebAuth
) -> None:
    resp = await client.post(
        "/settings/password",
        data={"current": "wrong", "password": "whatever12", "confirm": "whatever12"},
    )
    assert resp.status_code == 400
    assert auth.verify(PASSWORD)


async def test_settings_page_shows_connection_state(
    client: httpx.AsyncClient,
) -> None:
    page = await client.get("/settings")
    assert page.status_code == 200
    assert "Telegram" in page.text and "Not connected" in page.text

    await client.post(
        "/settings/platform/telegram",
        data={"token": "123:abc", "owner_id": "42"},
    )
    page = await client.get("/settings")
    assert "Connected" in page.text
    assert "123:abc" not in page.text  # secrets are never echoed back


async def test_health_page_lists_the_checklist(client: httpx.AsyncClient) -> None:
    page = await client.get("/health")
    assert page.status_code == 200
    assert "client-plane socket" in page.text
    assert "telegram adapter" in page.text
    assert "scheduler" in page.text
    assert "playwright sidecar" in page.text
