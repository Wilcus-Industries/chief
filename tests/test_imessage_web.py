"""Web settings iMessage panel (#156): whitelist management over real HTTP.

Real ASGI app + real DB rows through the :class:`IMessagePanel` seam; only the
auth session setup mirrors the existing web settings suite.
"""

from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.config import Settings
from chief.persistence import imessage as imessage_repo
from chief.web.app import IMessagePanel, WebDeps, build_web_app
from chief.web.auth import WebAuth
from chief.web.settings_io import OwnerConfig, SecretsStore, SettingsPanel

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
            imessage=IMessagePanel(session_factory=session_factory),
        )
    )
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://web"
    )
    resp = await http.post("/login", data={"password": PASSWORD})
    assert resp.status_code == 303
    return http


async def test_settings_page_shows_whitelist_and_unknown_senders(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    async with session_factory() as session:
        await imessage_repo.add_handle(
            session, handle="+15550000002", tier="guest",
            mode=imessage_repo.MODE_DRAFT,
        )
        await imessage_repo.record_unknown_sender(
            session,
            platform=imessage_repo.PLATFORM,
            handle="+15550000009",
            seen_at=datetime.now(UTC),
        )

    resp = await client.get("/settings")

    assert resp.status_code == 200
    assert "iMessage whitelist" in resp.text
    assert "+15550000002" in resp.text
    assert "draft-first" in resp.text
    assert "+15550000009" in resp.text  # unknown sender, allow-able in one tap


async def test_add_handle_whitelists_it_as_a_guest(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resp = await client.post(
        "/settings/imessage",
        data={"action": "add", "handle": "+1 (555) 000-0003", "draft": "on"},
    )

    assert resp.status_code == 303
    async with session_factory() as session:
        entries = await imessage_repo.list_whitelist(session)
    assert [(c.user_id, c.tier, p.mode if p else None) for c, p in entries] == [
        ("+15550000003", "guest", imessage_repo.MODE_DRAFT)
    ]


async def test_mode_flip_and_remove(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await imessage_repo.add_handle(
            session, handle="+15550000004", tier="guest"
        )

    resp = await client.post(
        "/settings/imessage",
        data={"action": "mode", "handle": "+15550000004", "mode": "draft"},
    )
    assert resp.status_code == 303
    async with session_factory() as session:
        pref = await imessage_repo.get_pref(session, "+15550000004")
    assert pref is not None and pref.mode == imessage_repo.MODE_DRAFT

    resp = await client.post(
        "/settings/imessage",
        data={"action": "remove", "handle": "+15550000004"},
    )
    assert resp.status_code == 303
    async with session_factory() as session:
        assert await imessage_repo.list_whitelist(session) == []


async def test_bad_requests_are_rejected(
    client: httpx.AsyncClient,
) -> None:
    assert (
        await client.post("/settings/imessage", data={"action": "add"})
    ).status_code == 400  # no handle
    assert (
        await client.post(
            "/settings/imessage",
            data={"action": "mode", "handle": "+15550000005", "mode": "bogus"},
        )
    ).status_code == 400
    assert (
        await client.post(
            "/settings/imessage",
            data={"action": "mode", "handle": "+15550000005", "mode": "draft"},
        )
    ).status_code == 400  # not whitelisted


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
    assert "iMessage whitelist" not in page.text
    resp = await http.post(
        "/settings/imessage", data={"action": "add", "handle": "+15550000006"}
    )
    assert resp.status_code == 404
