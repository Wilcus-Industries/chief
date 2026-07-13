"""Web UI boot wiring (#153): settings defaults and a real uvicorn bind.

The serve test binds a REAL TCP listener (ephemeral port) and drives it with a real
HTTP client — proving the embedded server, not just the ASGI app, comes up and shuts
down cleanly on the daemon's own loop.
"""

import asyncio
from contextlib import suppress
from pathlib import Path

import httpx
import pytest

from chief.config import Settings
from chief.web.app import WebDeps, build_web_app
from chief.web.auth import WebAuth
from chief.web.server import WebServer


def test_web_is_on_by_default_and_binds_localhost() -> None:
    settings = Settings()
    assert settings.web_enabled is True
    assert settings.web_lan_enabled is False
    assert settings.web_host == "127.0.0.1"


def test_lan_toggle_flips_the_bind_address() -> None:
    settings = Settings(web_lan_enabled=True)
    assert settings.web_host == "0.0.0.0"  # noqa: S104


def test_web_port_out_of_range_fails_the_boot() -> None:
    with pytest.raises(ValueError):
        Settings(web_port=0)
    with pytest.raises(ValueError):
        Settings(web_port=70000)


async def test_real_server_serves_login_and_stops(tmp_path: Path) -> None:
    auth = WebAuth(tmp_path / "secrets")
    auth.set_password("bootpassword")
    app = build_web_app(WebDeps(auth=auth))
    server = WebServer(app, host="127.0.0.1", port=0)
    task = asyncio.create_task(server.run())
    try:
        await asyncio.wait_for(server.started.wait(), 10)
        url = f"http://127.0.0.1:{server.bound_port}/login"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
        assert resp.status_code == 200
        assert "Log in" in resp.text
    finally:
        await server.stop()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
