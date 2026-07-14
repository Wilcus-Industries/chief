"""Web settings watches panel (#165): read-only, real DB rows over real HTTP.

Mirrors test_imessage_web.py's shape: a real ASGI app + real DB rows through the
:class:`WatchesPanel` seam; only the auth session setup mirrors the existing web
settings suite.
"""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.config import Settings
from chief.persistence import watches as watches_repo
from chief.web.app import WatchesPanel, WebDeps, build_web_app
from chief.web.auth import WebAuth
from chief.web.settings_io import OwnerConfig, SecretsStore, SettingsPanel

# Far in the future so an armed watch never reads as "expired" relative to real
# wall time, however far ahead the test suite happens to run.
_FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)

PASSWORD = "settingspassword"


@pytest.fixture
async def client(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> httpx.AsyncClient:
    auth = WebAuth(tmp_path / "secrets")
    auth.set_password(PASSWORD)
    app = build_web_app(
        WebDeps(
            auth=auth,
            settings_panel=SettingsPanel(
                secrets=SecretsStore(tmp_path / "secrets"),
                config=OwnerConfig(tmp_path / "config.yaml"),
                settings=Settings(),
            ),
            watches=WatchesPanel(session_factory=session_factory),
        )
    )
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://web"
    )
    resp = await http.post("/login", data={"password": PASSWORD})
    assert resp.status_code == 303
    return http


async def test_settings_page_shows_watch_fields(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await watches_repo.create_watch(
            session,
            target_handle="+15550000001",
            instruction="tell her I am late",
            expiry=_FAR_FUTURE,
            tone=watches_repo.TONE_SILENT,
        )

    resp = await client.get("/settings")

    assert resp.status_code == 200
    assert "Watches" in resp.text
    assert "+15550000001" in resp.text
    assert "tell her I am late" in resp.text
    assert "silent" in resp.text
    assert "armed" in resp.text


async def test_panel_hidden_when_imessage_is_off(tmp_path: Path) -> None:
    auth = WebAuth(tmp_path / "secrets2")
    auth.set_password(PASSWORD)
    app = build_web_app(
        WebDeps(
            auth=auth,
            settings_panel=SettingsPanel(
                secrets=SecretsStore(tmp_path / "secrets2"),
                config=OwnerConfig(tmp_path / "config2.yaml"),
                settings=Settings(),
            ),
        )
    )
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://web"
    )
    await http.post("/login", data={"password": PASSWORD})

    page = await http.get("/settings")
    assert "Watches" not in page.text
