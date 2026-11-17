"""MCP OAuth end to end against a local fake authorization + resource server.

Everything real except the far end: the SDK's own OAuth provider, a live
supervisor over a real streamable-HTTP transport, the token file on disk, the
broker's notices and audit trail. The fake (tests/fake_oauth.py) stands in for
the provider and "the owner's browser" is ``FakeOAuth.browser_approve``.
"""

import asyncio
import json
import logging
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chief.audit import AuditLog
from chief.mcpclient.manager import McpManager, ServerConfig
from chief.mcpclient.oauth import OAuthBroker
from chief.mcpclient.oauth_types import Surface
from chief.provider.base import ToolCall
from chief.tools import ToolRegistry

from .fake_oauth import FakeOAuth, serve_fake

SERVER = "rh"
REDIRECT = "http://127.0.0.1:8130/oauth/callback"
NOTICE_TO: Surface = ("imessage", "+15550001111")
NOTICE = 'Rh needs login. Say "connect rh" or /login rh.'
SURFACE: Surface = ("cli", "cli:t1")
LINK_SENT = "login link sent to the owner"
PING = ToolCall(id="1", name="mcp_rh_ping", arguments={})


@dataclass
class Rig:
    """One broker + manager over a shared on-disk state; ``reboot`` re-creates
    both over the same disk, like a daemon restart."""

    fake: FakeOAuth
    root: Path
    sent: list[tuple[Surface, str]]
    broker: OAuthBroker
    manager: McpManager
    registry: ToolRegistry
    config: ServerConfig
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def audit_kinds(self) -> list[str]:
        path = self.root / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["kind"] for line in path.read_text().splitlines()]

    async def reboot(self) -> "Rig":
        await self.manager.stop()
        return build(self.fake, self.root, self.sent, **self.options)


def build(
    fake: FakeOAuth, root: Path, sent: list[tuple[Surface, str]], **options: Any
) -> Rig:
    async def send(surface: Surface, text: str) -> None:
        sent.append((surface, text))

    broker = OAuthBroker(
        send=send,
        audit=AuditLog(root / "audit.jsonl"),
        notice_to=NOTICE_TO,
        redirect_uri=REDIRECT,
        token_dir=root / "tokens",
        notified_path=root / "notified.json",
        **options,
    )
    registry = ToolRegistry()
    config = ServerConfig(name=SERVER, url=fake.mcp_url, auth="oauth")
    manager = McpManager(registry, broker)
    return Rig(fake, root, sent, broker, manager, registry, config, options)


@pytest.fixture
async def fake() -> AsyncIterator[FakeOAuth]:
    async with serve_fake() as server:
        yield server


@pytest.fixture
async def rig(tmp_path: Path, fake: FakeOAuth) -> AsyncIterator[Rig]:
    current = build(fake, tmp_path, [])
    yield current
    await current.manager.stop()


def link_in(message: str) -> str:
    """The authorization link inside a login message."""
    (url,) = [line for line in message.splitlines() if line.startswith("http")]
    return url


async def connect_parked(rig: Rig) -> None:
    assert await asyncio.wait_for(rig.manager.connect(rig.config), 10) == 0


async def log_in(rig: Rig) -> str:
    """Park, ask for a login, approve it in 'the browser' and paste the address
    back. Returns the address that was pasted."""
    await connect_parked(rig)
    assert await rig.broker.start_login(SERVER, SURFACE) == LINK_SENT
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
    result = await rig.broker.complete_login(callback, SERVER)
    assert result.ok, result.message
    return callback


# -- the passive path --------------------------------------------------------


async def test_no_login_parks_the_server_and_tells_the_owner_once(rig: Rig) -> None:
    count = await asyncio.wait_for(rig.manager.connect(rig.config), 10)

    assert count == 0  # boot is not held up
    assert rig.broker.state(SERVER) == "needs_auth"
    assert rig.broker.needs_auth() == [SERVER]
    assert rig.sent == [(NOTICE_TO, NOTICE)]
    assert rig.registry.specs() == []
    assert rig.audit_kinds == ["mcp_auth_needed"]
    # The passive attempt registered the client, so a login skips that step.
    assert len(rig.fake.registrations) == 1


async def test_a_restart_with_no_login_does_not_repeat_the_notice(rig: Rig) -> None:
    await asyncio.wait_for(rig.manager.connect(rig.config), 10)

    again = await rig.reboot()
    try:
        assert await asyncio.wait_for(again.manager.connect(again.config), 10) == 0
        assert again.broker.state(SERVER) == "needs_auth"
    finally:
        await again.manager.stop()

    assert rig.sent == [(NOTICE_TO, NOTICE)]  # still just the one
    assert len(rig.fake.registrations) == 1  # and no second registration


async def test_a_login_wall_is_not_logged_as_an_error(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    # The SDK logs whatever its flow raises as an ERROR with a traceback; the
    # wall is an expected state, not a failure worth that on every boot.
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(rig.manager.connect(rig.config), 10)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


# -- the interactive login ---------------------------------------------------


async def test_start_login_sends_the_link_to_the_asking_surface_only(
    rig: Rig,
) -> None:
    await connect_parked(rig)

    reply = await rig.broker.start_login(SERVER, SURFACE)

    assert reply == LINK_SENT  # all the model is ever told
    surface, text = rig.sent[-1]
    assert surface == SURFACE
    assert text.startswith("Open this link and log in to Rh:\n")
    assert "http://127.0.0.1" in text  # the paste-back instructions
    assert link_in(text).startswith(f"{rig.fake.base_url}/authorize?")
    assert "http" not in reply


async def test_pasting_the_address_back_connects_with_tools_live(rig: Rig) -> None:
    await connect_parked(rig)
    await rig.broker.start_login(SERVER, SURFACE)
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
    assert callback.startswith(REDIRECT)

    result = await rig.broker.complete_login(callback, SERVER)

    assert (result.ok, result.message) == (True, "rh connected (1 tool)")
    assert rig.broker.state(SERVER) == "connected"
    assert rig.broker.needs_auth() == []
    # Tools appear without a restart, and calls carry the new bearer token.
    assert {spec.name for spec in rig.registry.specs()} == {"mcp_rh_ping"}
    assert await rig.registry.dispatch(PING) == "pong"
    # The login is on disk, and the notice flag is cleared.
    stored = json.loads((rig.root / "tokens" / "rh.json").read_text())
    assert stored["tokens"]["access_token"] and stored["client_info"]["client_id"]
    assert json.loads((rig.root / "notified.json").read_text()) == []
    kinds = ["mcp_auth_needed", "mcp_auth_start", "mcp_auth_complete"]
    assert rig.audit_kinds == kinds


async def test_neither_the_code_nor_the_tokens_reach_the_audit_log_or_notices(
    rig: Rig,
) -> None:
    callback = await log_in(rig)
    code = callback.split("code=")[1].split("&")[0]
    token = json.loads((rig.root / "tokens" / "rh.json").read_text())["tokens"]
    audit = (rig.root / "audit.jsonl").read_text()
    for secret in (code, token["access_token"], token["refresh_token"]):
        assert secret not in audit
    assert all(code not in text for _, text in rig.sent)


async def test_the_login_is_a_pkce_s256_public_client_on_the_loopback_redirect(
    rig: Rig,
) -> None:
    await log_in(rig)
    (request,) = rig.fake.authorize_requests
    assert request["code_challenge_method"] == "S256"
    assert request["redirect_uri"] == REDIRECT
    (registration,) = rig.fake.registrations
    assert registration["token_endpoint_auth_method"] == "none"
    assert registration["redirect_uris"] == [REDIRECT]
    assert registration["client_name"] == "chief"


async def test_configured_scopes_override_the_ones_the_server_advertises(
    tmp_path: Path, fake: FakeOAuth
) -> None:
    rig = build(fake, tmp_path, [])
    config = ServerConfig(
        name=SERVER, url=fake.mcp_url, auth="oauth", scopes=("alpha", "beta")
    )
    try:
        await asyncio.wait_for(rig.manager.connect(config), 10)
        await rig.broker.start_login(SERVER, SURFACE)
    finally:
        await rig.manager.stop()
    assert link_in(rig.sent[-1][1]).count("scope=alpha+beta") == 1


async def test_a_second_login_request_replaces_the_first(rig: Rig) -> None:
    await connect_parked(rig)
    await rig.broker.start_login(SERVER, SURFACE)
    first = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
    await rig.broker.start_login(SERVER, ("web", "web:main"))
    assert rig.sent[-1][0] == ("web", "web:main")  # the new link, to its asker
    second = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))

    old = await rig.broker.complete_login(first, SERVER)
    new = await rig.broker.complete_login(second, SERVER)

    assert not old.ok and "expired or not recognized" in old.message
    assert new.ok, new.message


async def test_an_address_with_the_wrong_state_completes_nothing(rig: Rig) -> None:
    await connect_parked(rig)
    await rig.broker.start_login(SERVER, SURFACE)
    forged = f"{REDIRECT}?code=attacker-code&state=not-the-state"

    result = await rig.broker.complete_login(forged, SERVER)

    assert not result.ok
    assert result.message == "flow expired or not recognized — /login rh again"
    assert rig.fake.token_requests == []
    # The real link is still good: a stranger's guess does not burn it.
    real = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
    assert (await rig.broker.complete_login(real, SERVER)).ok


async def test_an_address_without_a_state_or_with_no_open_login_is_refused(
    rig: Rig,
) -> None:
    await connect_parked(rig)
    for url in (f"{REDIRECT}?code=x", f"{REDIRECT}?code=x&state=y", "garbage"):
        result = await rig.broker.complete_login(url)
        assert not result.ok and "expired or not recognized" in result.message


async def test_a_login_link_expires_after_its_ttl(
    tmp_path: Path, fake: FakeOAuth
) -> None:
    now = [1000.0]
    rig = build(fake, tmp_path, [], clock=lambda: now[0])
    try:
        await connect_parked(rig)
        await rig.broker.start_login(SERVER, SURFACE)
        callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
        now[0] += 601  # the link was good for ten minutes

        result = await rig.broker.complete_login(callback, SERVER)

        assert not result.ok and "expired or not recognized" in result.message
        assert rig.fake.token_requests == []
        # The attempt ended cleanly: the owner can simply ask again.
        assert await rig.broker.start_login(SERVER, SURFACE) == LINK_SENT
        fresh = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
        assert (await rig.broker.complete_login(fresh, SERVER)).ok
    finally:
        await rig.manager.stop()


async def test_an_unanswered_link_times_out_and_the_owner_can_ask_again(
    tmp_path: Path, fake: FakeOAuth
) -> None:
    rig = build(fake, tmp_path, [], flow_ttl=0.3)
    try:
        await connect_parked(rig)
        assert await rig.broker.start_login(SERVER, SURFACE) == LINK_SENT
        await asyncio.sleep(0.6)  # the link's whole life, unanswered

        assert rig.broker.state(SERVER) == "needs_auth"
        assert await rig.broker.start_login(SERVER, SURFACE) == LINK_SENT
    finally:
        await rig.manager.stop()


async def test_the_server_declining_the_login_is_reported_clearly(rig: Rig) -> None:
    rig.fake.deny_authorize = True
    await connect_parked(rig)
    await rig.broker.start_login(SERVER, SURFACE)
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
    assert "error=access_denied" in callback

    result = await rig.broker.complete_login(callback, SERVER)

    assert not result.ok
    assert "login denied (access_denied)" in result.message
    assert rig.broker.state(SERVER) == "needs_auth"
    assert rig.audit_kinds[-1] == "mcp_auth_fail"


async def test_a_failed_code_exchange_leaves_no_tokens_and_can_be_retried(
    rig: Rig,
) -> None:
    rig.fake.exchange_error = "invalid_grant"
    await connect_parked(rig)
    await rig.broker.start_login(SERVER, SURFACE)
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))

    result = await rig.broker.complete_login(callback, SERVER)

    assert not result.ok and "login failed" in result.message
    assert "tokens" not in json.loads((rig.root / "tokens" / "rh.json").read_text())
    assert rig.broker.state(SERVER) == "needs_auth"

    rig.fake.exchange_error = None
    await rig.broker.start_login(SERVER, SURFACE)
    again = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
    assert (await rig.broker.complete_login(again, SERVER)).ok


async def test_login_requests_for_servers_that_cannot_log_in_are_refused(
    rig: Rig,
) -> None:
    assert "not an OAuth MCP server" in await rig.broker.start_login("nope", SURFACE)
    await log_in(rig)
    assert await rig.broker.start_login(SERVER, SURFACE) == "rh is already connected"


async def test_a_login_while_still_connecting_is_asked_to_wait(rig: Rig) -> None:
    rig.broker.register(rig.config)  # known, but its first attempt has not landed
    assert "still connecting" in await rig.broker.start_login(SERVER, SURFACE)


# -- surviving a restart -----------------------------------------------------


def age_the_stored_token(root: Path) -> None:
    path = root / "tokens" / "rh.json"
    record = json.loads(path.read_text())
    record["expires_at"] = time.time() - 10
    path.write_text(json.dumps(record))


async def test_a_restart_with_an_unexpired_login_connects_without_a_prompt(
    rig: Rig,
) -> None:
    await log_in(rig)
    sent_before = list(rig.sent)

    again = await rig.reboot()
    try:
        assert await asyncio.wait_for(again.manager.connect(again.config), 10) == 1
        assert again.broker.state(SERVER) == "connected"
    finally:
        await again.manager.stop()
    assert rig.sent == sent_before  # no notice, no link
    assert [r["grant_type"] for r in rig.fake.token_requests] == ["authorization_code"]


async def test_an_expired_access_token_refreshes_after_a_restart_not_a_new_login(
    rig: Rig,
) -> None:
    await log_in(rig)
    age_the_stored_token(rig.root)

    again = await rig.reboot()
    try:
        assert await asyncio.wait_for(again.manager.connect(again.config), 10) == 1
        assert again.broker.state(SERVER) == "connected"
        assert await again.registry.dispatch(PING) == "pong"
    finally:
        await again.manager.stop()

    grants = [r["grant_type"] for r in rig.fake.token_requests]
    assert grants == ["authorization_code", "refresh_token"]
    assert len(rig.sent) == 2  # the first notice and the link: nothing since


async def test_a_refresh_that_returns_no_new_refresh_token_keeps_working(
    rig: Rig,
) -> None:
    rig.fake.rotate_refresh = False
    await log_in(rig)
    for _ in range(2):  # two restarts, each needing a refresh off the same token
        age_the_stored_token(rig.root)
        rig = await rig.reboot()
        assert await asyncio.wait_for(rig.manager.connect(rig.config), 10) == 1
    await rig.manager.stop()
    grants = [r["grant_type"] for r in rig.fake.token_requests]
    assert grants == ["authorization_code", "refresh_token", "refresh_token"]


async def test_a_dead_refresh_token_after_a_restart_parks_the_server_again(
    rig: Rig,
) -> None:
    await log_in(rig)
    age_the_stored_token(rig.root)
    rig.fake.refresh_error = "invalid_grant"
    rig.sent.clear()

    again = await rig.reboot()
    try:
        assert await asyncio.wait_for(again.manager.connect(again.config), 10) == 0
        assert again.broker.state(SERVER) == "needs_auth"
    finally:
        await again.manager.stop()
    assert rig.sent == [(NOTICE_TO, NOTICE)]


async def test_a_changed_server_url_ignores_the_stored_login(
    rig: Rig, tmp_path: Path
) -> None:
    await log_in(rig)
    await rig.manager.stop()

    async with serve_fake() as elsewhere:
        moved = build(elsewhere, tmp_path, rig.sent)
        try:
            assert await asyncio.wait_for(moved.manager.connect(moved.config), 10) == 0
            assert moved.broker.state(SERVER) == "needs_auth"
        finally:
            await moved.manager.stop()
        assert len(elsewhere.registrations) == 1  # a fresh registration, not a reuse


async def test_the_authorization_server_being_down_is_a_failure_not_a_login_wall(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        rig.fake.as_base = f"http://127.0.0.1:{probe.getsockname()[1]}"

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="failed to start"):
            await asyncio.wait_for(rig.manager.connect(rig.config), 10)

    assert rig.broker.state(SERVER) == "disabled"
    assert rig.sent == []  # nobody is asked to log in over a network error
    assert "mcp_auth_needed" not in rig.audit_kinds
    assert "rh" in caplog.text


# -- a login lost mid-session ------------------------------------------------


def run_the_sdk_clock_forward(
    monkeypatch: pytest.MonkeyPatch, seconds: float
) -> None:
    """Make the SDK's provider believe ``seconds`` have passed (its tokens'
    expiry is judged on its own clock)."""
    real = time.time
    stand_in = SimpleNamespace(time=lambda: real() + seconds)
    monkeypatch.setattr("mcp.client.auth.oauth2.time", stand_in)


async def test_a_login_lost_mid_session_parks_the_server_and_answers_the_call(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await log_in(rig)
    rig.sent.clear()
    run_the_sdk_clock_forward(monkeypatch, 7200)  # the access token has run out
    rig.fake.refresh_error = "invalid_grant"  # and the refresh token is dead

    reply = await asyncio.wait_for(rig.registry.dispatch(PING), 10)

    assert reply == "error: rh needs login; call server_login"
    assert rig.broker.state(SERVER) == "needs_auth"
    assert rig.sent == [(NOTICE_TO, NOTICE)]
    # Later calls fail fast the same way, without touching the server again.
    seen = len(rig.fake.seen_headers)
    assert await rig.registry.dispatch(PING) == reply
    assert len(rig.fake.seen_headers) == seen


async def test_a_login_the_server_revokes_mid_session_parks_it_and_can_be_redone(
    rig: Rig,
) -> None:
    await log_in(rig)
    rig.sent.clear()
    rig.fake.revoke_access_tokens()  # the server stops honouring the token
    rig.fake.refresh_error = "invalid_grant"

    reply = await asyncio.wait_for(rig.registry.dispatch(PING), 10)

    assert reply == "error: rh needs login; call server_login"
    assert rig.sent == [(NOTICE_TO, NOTICE)]
    rig.fake.refresh_error = None
    assert await rig.broker.start_login(SERVER, SURFACE) == LINK_SENT
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
    assert (await rig.broker.complete_login(callback, SERVER)).ok
    assert await asyncio.wait_for(rig.registry.dispatch(PING), 10) == "pong"


async def test_a_login_request_that_a_refresh_can_answer_just_connects(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await log_in(rig)
    run_the_sdk_clock_forward(monkeypatch, 7200)
    rig.fake.refresh_error = "invalid_grant"  # a hiccup, not a real revocation
    await asyncio.wait_for(rig.registry.dispatch(PING), 10)
    rig.fake.refresh_error = None
    monkeypatch.undo()
    age_the_stored_token(rig.root)
    sent = len(rig.sent)

    assert await rig.broker.start_login(SERVER, SURFACE) == "rh connected (1 tool)"

    assert len(rig.sent) == sent  # no link: the refresh token still worked
    assert await asyncio.wait_for(rig.registry.dispatch(PING), 10) == "pong"
