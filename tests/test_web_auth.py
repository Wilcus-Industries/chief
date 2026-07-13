"""Web UI auth tests (#153): setup, login, sessions — through the real ASGI app.

Every test drives real HTTP requests (httpx over ASGITransport) against the real
Starlette app; nothing HTTP-level is mocked. The credential store is the real
:class:`WebAuth` over a tmp secrets dir.
"""

import stat
from pathlib import Path

import httpx
import pytest

from chief.web.app import WebDeps, build_web_app
from chief.web.auth import (
    PASSWORD_FILE,
    SESSION_COOKIE,
    SESSIONS_FILE,
    WebAuth,
    hash_password,
    verify_password,
)


@pytest.fixture
def auth(tmp_path: Path) -> WebAuth:
    return WebAuth(tmp_path / "secrets")


@pytest.fixture
def client(auth: WebAuth) -> httpx.AsyncClient:
    app = build_web_app(WebDeps(auth=auth))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://web")


def test_password_hash_roundtrip_and_shape() -> None:
    stored = hash_password("hunter2boogaloo")
    assert stored.startswith("scrypt$")
    assert "hunter2boogaloo" not in stored
    assert verify_password("hunter2boogaloo", stored)
    assert not verify_password("wrong", stored)


def test_verify_rejects_malformed_stored_hash() -> None:
    assert not verify_password("anything", "not-a-hash")
    assert not verify_password("anything", "scrypt$bad$fields")


async def test_fresh_install_redirects_to_setup(client: httpx.AsyncClient) -> None:
    resp = await client.get("/chat")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"


async def test_setup_sets_password_logs_in_and_hashes_at_rest(
    client: httpx.AsyncClient, auth: WebAuth, tmp_path: Path
) -> None:
    resp = await client.post(
        "/setup", data={"password": "opensesame1", "confirm": "opensesame1"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/chat"
    assert SESSION_COOKIE in resp.cookies

    password_file = tmp_path / "secrets" / PASSWORD_FILE
    stored = password_file.read_text()
    assert stored.startswith("scrypt$")
    assert "opensesame1" not in stored
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    sessions_file = tmp_path / "secrets" / SESSIONS_FILE
    assert stat.S_IMODE(sessions_file.stat().st_mode) == 0o600


async def test_setup_refused_once_a_password_exists(
    client: httpx.AsyncClient, auth: WebAuth
) -> None:
    auth.set_password("alreadyset123")
    resp = await client.post(
        "/setup", data={"password": "attacker", "confirm": "attacker"}
    )
    assert resp.status_code == 409
    assert auth.verify("alreadyset123")


async def test_setup_rejects_short_or_mismatched_password(
    client: httpx.AsyncClient, auth: WebAuth
) -> None:
    resp = await client.post("/setup", data={"password": "short", "confirm": "short"})
    assert resp.status_code == 400
    assert not auth.password_set
    resp = await client.post(
        "/setup", data={"password": "longenough1", "confirm": "different1"}
    )
    assert resp.status_code == 400
    assert not auth.password_set


async def test_login_wrong_password_sets_no_cookie(
    client: httpx.AsyncClient, auth: WebAuth
) -> None:
    auth.set_password("rightpassword")
    resp = await client.post("/login", data={"password": "wrongpassword"})
    assert resp.status_code == 401
    assert SESSION_COOKIE not in resp.cookies


async def test_login_then_authenticated_page_then_logout(
    client: httpx.AsyncClient, auth: WebAuth
) -> None:
    auth.set_password("rightpassword")
    resp = await client.post("/login", data={"password": "rightpassword"})
    assert resp.status_code == 303
    assert SESSION_COOKIE in resp.cookies

    resp = await client.get("/")  # cookie jar carries the session
    assert resp.status_code == 303
    assert resp.headers["location"] == "/chat"

    resp = await client.post("/logout")
    assert resp.status_code == 303
    resp = await client.get("/chat")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_session_survives_an_app_rebuild(
    client: httpx.AsyncClient, auth: WebAuth, tmp_path: Path
) -> None:
    # The cookie is persistent and the token store is on disk, so a daemon restart
    # (a fresh app over the same secrets dir) keeps the owner logged in.
    auth.set_password("rightpassword")
    resp = await client.post("/login", data={"password": "rightpassword"})
    token = resp.cookies[SESSION_COOKIE]

    rebuilt = build_web_app(WebDeps(auth=WebAuth(tmp_path / "secrets")))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rebuilt),
        base_url="http://web",
        cookies={SESSION_COOKIE: token},
    ) as second:
        resp = await second.get("/chat")
        assert resp.status_code == 200


async def test_password_change_revokes_every_session(auth: WebAuth) -> None:
    auth.set_password("firstpassword")
    token = auth.issue_session()
    assert auth.session_valid(token)
    auth.set_password("secondpassword")
    assert not auth.session_valid(token)


async def test_garbage_cookie_is_rejected(
    client: httpx.AsyncClient, auth: WebAuth
) -> None:
    auth.set_password("rightpassword")
    client.cookies.set(SESSION_COOKIE, "forged-token")
    resp = await client.get("/chat")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
