"""MCP OAuth through the booted daemon: production wiring, a real web listener
and socket client, a local fake provider. Only the LLM is swapped."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from chief.app import App, build_app
from chief.config import Config

from .fake_oauth import FakeOAuth, serve_fake
from .fakes import FakeProvider, text_turn, tool_turn
from .test_app import Streams, boot, make_config, read_finals, send_frame, shutdown

NOTICE = 'Rh needs login. Say "connect rh" or /login rh.'
LINK_SENT = "login link sent to the owner"


@pytest.fixture
async def fake() -> AsyncIterator[FakeOAuth]:
    async with serve_fake() as server:
        yield server


def oauth_config(
    tmp_path: Path, sock_path: Path, fake: FakeOAuth, **overrides: object
) -> Config:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return make_config(
        tmp_path,
        sock_path,
        mcp_servers={"rh": {"url": fake.mcp_url, "auth": "oauth"}},
        web_password="hunter2",  # the web listener the login callback lands on
        web_port=port,
        **overrides,
    )


async def boot_watching_web(
    config: Config, provider: FakeProvider
) -> tuple[App, Streams, asyncio.Queue[dict[str, object]]]:
    """Boot, with a listener already on the web owner thread so the boot-time
    notice (sent while ``start`` is still running) is not missed."""
    app = await build_app(config, provider=provider)
    web_main = app.hub.listen("web:main")
    await app.start()
    streams = await asyncio.open_unix_connection(str(config.socket_path))
    return app, streams, web_main


async def stop(app: App, streams: Streams) -> None:
    streams[1].close()
    await app.stop()


def link_in(text: str) -> str:
    (url,) = [line for line in text.splitlines() if line.startswith("http")]
    return url


@pytest.fixture(autouse=True)
def private_token_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chief.mcpclient.auth_tools.TOKEN_DIR", tmp_path / "tokens")


async def test_an_install_with_no_oauth_server_gains_no_tools_or_commands(
    tmp_path: Path, sock_path: Path
) -> None:
    provider = FakeProvider([text_turn("hello"), text_turn("unused")])
    app, streams = await boot(make_config(tmp_path, sock_path), provider)
    try:
        send_frame(streams, "hi")
        await read_finals(streams, 1)
        offered = {spec.name for spec in provider.tool_specs[0]}
        assert not {"server_login", "server_login_complete"} & offered

        send_frame(streams, "/login rh")
        (reply,) = await read_finals(streams, 1)
        assert reply["text"] == "unknown command /login — try /help"
    finally:
        await shutdown(app, streams)


async def test_boot_parks_the_server_tells_the_owner_and_hints_the_model(
    tmp_path: Path, sock_path: Path, fake: FakeOAuth
) -> None:
    provider = FakeProvider([text_turn("hello")])
    config = oauth_config(tmp_path, sock_path, fake)
    app, streams, web_main = await boot_watching_web(config, provider)
    try:
        notice = await asyncio.wait_for(web_main.get(), 5)
        assert notice == {"type": "final", "thread": "web:main", "text": NOTICE}

        send_frame(streams, "hi")
        await read_finals(streams, 1)
        system = json.dumps(provider.calls[0])
        assert "MCP servers needing login: rh" in system
        assert "mcp_rh_ping" not in {s.name for s in provider.tool_specs[0]}
    finally:
        await stop(app, streams)


async def test_the_owner_logs_in_with_the_slash_commands_and_the_server_goes_live(
    tmp_path: Path, sock_path: Path, fake: FakeOAuth
) -> None:
    provider = FakeProvider(
        [
            tool_turn("mcp_rh_ping", {}),
            text_turn("CLEAN"),  # the core injection screen's fallback verdict
            text_turn("ponged"),
        ]
    )
    config = oauth_config(tmp_path, sock_path, fake, gate_approved=("mcp_rh_ping",))
    app, streams, _ = await boot_watching_web(config, provider)
    try:
        send_frame(streams, "/login rh")
        link, reply = await read_finals(streams, 2)
        assert reply["text"] == LINK_SENT
        callback = await fake.browser_approve(link_in(link["text"]))
        assert callback.startswith(f"http://127.0.0.1:{config.web_port}/oauth/callback")

        send_frame(streams, f"/login rh {callback}")
        (done,) = await read_finals(streams, 1)
        assert done["text"] == "rh connected (1 tool)"

        send_frame(streams, "ping it")
        (final,) = await read_finals(streams, 1)
        assert final["text"] == "ponged"
        assert provider.calls[1][-1]["content"] == "pong"
        # The hint is gone now that nothing is waiting on a login.
        assert "MCP servers needing login" not in json.dumps(provider.calls[-1])
    finally:
        await stop(app, streams)


async def test_a_login_finished_in_the_browser_lands_on_the_real_web_listener(
    tmp_path: Path, sock_path: Path, fake: FakeOAuth
) -> None:
    config = oauth_config(tmp_path, sock_path, fake)
    app, streams, _ = await boot_watching_web(config, FakeProvider([]))
    try:
        send_frame(streams, "/login rh")
        link, _ = await read_finals(streams, 2)
        callback = await fake.browser_approve(link_in(link["text"]))

        async with httpx.AsyncClient() as browser:  # no cookie, no password
            response = await browser.get(callback)
            forged = await browser.get(
                f"http://127.0.0.1:{config.web_port}/oauth/callback?code=x&state=y"
            )

        assert response.status_code == 200
        assert "rh connected (1 tool)" in response.text
        assert forged.status_code == 400
    finally:
        await stop(app, streams)


async def test_the_model_starts_and_finishes_a_login_without_ever_seeing_the_link(
    tmp_path: Path, sock_path: Path, fake: FakeOAuth
) -> None:
    pasted = {"server": "rh", "callback_url": ""}  # filled in once the owner replies
    provider = FakeProvider(
        [
            tool_turn("server_login", {"server": "rh"}),
            text_turn("I sent you a login link"),
            tool_turn("server_login_complete", pasted, call_id="call-2"),
            text_turn("you're connected"),
        ]
    )
    config = oauth_config(tmp_path, sock_path, fake)
    app, streams, _ = await boot_watching_web(config, provider)
    try:
        send_frame(streams, "connect rh")
        # No approval card: server_login is approved by core, not by config.
        link, said = await read_finals(streams, 2)
        assert said["text"] == "I sent you a login link"
        assert link["thread"] == "cli:t1"
        url = link_in(link["text"])
        assert provider.calls[1][-1]["content"] == LINK_SENT
        assert url not in json.dumps(provider.calls)  # the model never saw it

        pasted["callback_url"] = await fake.browser_approve(url)
        send_frame(streams, "here is the address it ended on")
        (done,) = await read_finals(streams, 1)
        assert done["text"] == "you're connected"
        assert provider.calls[-1][-1]["content"] == "rh connected (1 tool)"
    finally:
        await stop(app, streams)
