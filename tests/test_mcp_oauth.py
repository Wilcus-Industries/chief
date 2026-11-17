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
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from chief.adapters.base import Message
from chief.audit import AuditLog
from chief.commands import CommandHandler
from chief.hooks.context import TurnContext
from chief.hooks.registry import HookRegistry
from chief.mcpclient.auth_tools import register_auth_surface
from chief.mcpclient.manager import McpManager, ServerConfig
from chief.mcpclient.oauth import OAuthBroker
from chief.mcpclient.oauth_types import Surface
from chief.provider.base import ToolCall
from chief.tools import ToolContext, ToolRegistry
from chief.web.oauth_routes import build_oauth_routes

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
    fake: FakeOAuth,
    root: Path,
    sent: list[tuple[Surface, str]],
    dead_channel: bool = False,
    **options: Any,
) -> Rig:
    async def send(surface: Surface, text: str) -> None:
        if dead_channel:
            raise ConnectionError("the owner's channel is down")
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


async def eventually(check: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait (bounded) for something that happens on another task — the thing
    asserted, not a stand-in for it."""
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


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


async def test_a_notice_that_could_not_be_sent_is_retried_on_the_next_boot(
    tmp_path: Path, fake: FakeOAuth, caplog: pytest.LogCaptureFixture
) -> None:
    sent: list[tuple[Surface, str]] = []
    down = build(fake, tmp_path, sent, dead_channel=True)
    try:
        with caplog.at_level(logging.ERROR):
            assert await asyncio.wait_for(down.manager.connect(down.config), 10) == 0
    finally:
        await down.manager.stop()
    assert sent == [] and "could not tell the owner" in caplog.text

    up = build(fake, tmp_path, sent)
    try:
        assert await asyncio.wait_for(up.manager.connect(up.config), 10) == 0
    finally:
        await up.manager.stop()
    assert sent == [(NOTICE_TO, NOTICE)]


async def test_an_unreadable_notice_file_is_ignored_not_fatal(
    tmp_path: Path, fake: FakeOAuth
) -> None:
    (tmp_path / "notified.json").write_text("{nope")
    rig = build(fake, tmp_path, [])
    try:
        assert await asyncio.wait_for(rig.manager.connect(rig.config), 10) == 0
    finally:
        await rig.manager.stop()
    assert rig.sent == [(NOTICE_TO, NOTICE)]


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
        await eventually(lambda: "mcp_auth_fail" in rig.audit_kinds)  # it lapsed

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


async def test_stopping_with_a_login_link_outstanding_ends_cleanly(rig: Rig) -> None:
    await connect_parked(rig)
    assert await rig.broker.start_login(SERVER, SURFACE) == LINK_SENT

    await asyncio.wait_for(rig.manager.stop(), 5)

    leaked = [
        task
        for task in asyncio.all_tasks()
        if "supervise" in getattr(task.get_coro(), "__qualname__", "")
    ]
    assert leaked == []


async def test_two_servers_are_parked_and_logged_in_independently(rig: Rig) -> None:
    async with serve_fake() as second:
        other = ServerConfig(name="gh", url=second.mcp_url, auth="oauth")
        try:
            await connect_parked(rig)
            assert await asyncio.wait_for(rig.manager.connect(other), 10) == 0
            assert rig.broker.needs_auth() == ["gh", "rh"]
            assert [text for _, text in rig.sent] == [
                NOTICE,
                'Gh needs login. Say "connect gh" or /login gh.',
            ]

            await rig.broker.start_login("gh", SURFACE)
            callback = await second.browser_approve(link_in(rig.sent[-1][1]))
            # The address is gh's: it cannot finish a login for rh.
            assert not (await rig.broker.complete_login(callback, SERVER)).ok
            assert (await rig.broker.complete_login(callback)).ok

            assert rig.broker.state("gh") == "connected"
            assert rig.broker.state(SERVER) == "needs_auth"
            assert rig.broker.needs_auth() == [SERVER]
        finally:
            await rig.manager.stop()


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


async def test_a_session_started_from_stored_tokens_refreshes_in_place_at_expiry(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await log_in(rig)
    again = await rig.reboot()
    try:
        assert await asyncio.wait_for(again.manager.connect(again.config), 10) == 1
        run_the_sdk_clock_forward(monkeypatch, 7200)  # the restored expiry passes

        assert await asyncio.wait_for(again.registry.dispatch(PING), 10) == "pong"
        assert again.broker.state(SERVER) == "connected"
    finally:
        await again.manager.stop()
    grants = [r["grant_type"] for r in rig.fake.token_requests]
    assert grants == ["authorization_code", "refresh_token"]
    assert len(rig.sent) == 2  # no notice: nothing was ever wrong


async def test_the_refresh_token_only_ever_goes_to_the_advertised_token_endpoint(
    rig: Rig,
) -> None:
    await log_in(rig)
    for _ in range(2):  # two restarts: each builds a provider that has not discovered
        age_the_stored_token(rig.root)
        rig = await rig.reboot()
        assert await asyncio.wait_for(rig.manager.connect(rig.config), 10) == 1
    await rig.manager.stop()

    # The SDK's fallback when it has no metadata is <mcp origin>/token, a host
    # and path the server never named; the refresh token must not be sent there.
    assert "/token" not in {path for _, path in rig.fake.requests}
    grants = [r["grant_type"] for r in rig.fake.token_requests]
    assert grants == ["authorization_code", "refresh_token", "refresh_token"]


async def test_a_refresh_after_a_restart_names_the_same_resource_as_the_login(
    rig: Rig,
) -> None:
    # The SDK adds the RFC 8707 resource only once it knows the server's
    # protected-resource metadata (or a protocol version, which the first request
    # after a restart has not got yet); the stored metadata keeps that refresh the
    # same grant the login made.
    await log_in(rig)
    age_the_stored_token(rig.root)

    again = await rig.reboot()
    try:
        assert await asyncio.wait_for(again.manager.connect(again.config), 10) == 1
    finally:
        await again.manager.stop()

    exchange, refresh = rig.fake.token_requests
    assert refresh["resource"] == exchange["resource"] == rig.fake.mcp_url


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


async def test_a_struggling_token_endpoint_at_boot_fails_the_start_not_the_login(
    rig: Rig,
) -> None:
    await log_in(rig)
    age_the_stored_token(rig.root)
    token_file = rig.root / "tokens" / "rh.json"
    saved, kinds = token_file.read_text(), rig.audit_kinds
    rig.fake.refresh_status = 503
    rig.sent.clear()

    again = await rig.reboot()
    try:
        with pytest.raises(RuntimeError, match="failed to start"):
            await asyncio.wait_for(again.manager.connect(again.config), 10)
        assert again.broker.state(SERVER) == "disabled"  # like any server down at boot
    finally:
        await again.manager.stop()

    assert rig.sent == []  # nobody is asked to log in over a blip
    assert rig.audit_kinds == kinds
    assert token_file.read_text() == saved
    # The login is still there: the next boot, once the endpoint is back, uses it.
    rig.fake.refresh_status = None
    later = await again.reboot()
    try:
        assert await asyncio.wait_for(later.manager.connect(later.config), 10) == 1
    finally:
        await later.manager.stop()
    assert len(rig.fake.authorize_requests) == 1  # no second login


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
    """Make the SDK's provider believe ``seconds`` have passed: it judges a
    token's expiry on its own clock, and computes a new one's on ``auth_utils``'."""
    real = time.time
    stand_in = SimpleNamespace(time=lambda: real() + seconds)
    monkeypatch.setattr("mcp.client.auth.oauth2.time", stand_in)
    monkeypatch.setattr("mcp.shared.auth_utils.time", stand_in)


async def park_on_a_hiccup(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Log in, then let the token endpoint refuse one refresh: the server parks,
    though its stored refresh token is still good (a hiccup, not a revocation).
    The stored access token is left aged, so the next connection refreshes it."""
    await log_in(rig)
    run_the_sdk_clock_forward(monkeypatch, 7200)
    rig.fake.refresh_error = "invalid_grant"
    await asyncio.wait_for(rig.registry.dispatch(PING), 10)  # parks the server
    rig.fake.refresh_error = None
    monkeypatch.undo()
    age_the_stored_token(rig.root)


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


@pytest.mark.parametrize("status", [400, 401])
async def test_a_token_endpoint_that_refuses_the_refresh_parks_the_server_for_a_login(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    await log_in(rig)
    rig.sent.clear()
    run_the_sdk_clock_forward(monkeypatch, 7200)
    rig.fake.refresh_status = status

    reply = await asyncio.wait_for(rig.registry.dispatch(PING), 10)

    assert reply == "error: rh needs login; call server_login"
    assert rig.broker.state(SERVER) == "needs_auth"
    assert rig.sent == [(NOTICE_TO, NOTICE)]


@pytest.mark.parametrize("status", [500, 503, 429])
async def test_a_struggling_token_endpoint_keeps_the_login_and_the_server_reconnects(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    await log_in(rig)
    token_file = rig.root / "tokens" / "rh.json"
    saved, kinds = token_file.read_text(), rig.audit_kinds
    rig.sent.clear()
    monkeypatch.setattr("chief.mcpclient.supervisor.RECONNECT_DELAY_SECONDS", 0.05)
    run_the_sdk_clock_forward(monkeypatch, 7200)  # the access token has run out
    rig.fake.refresh_status = status  # and the endpoint that renews it is struggling

    with caplog.at_level(logging.ERROR):
        reply = await asyncio.wait_for(rig.registry.dispatch(PING), 10)

    # Not a login problem: an ordinary dropped connection, the login untouched.
    assert reply == "error: mcp server 'rh' is not connected"
    assert rig.broker.state(SERVER) != "needs_auth"
    assert rig.sent == []
    assert rig.audit_kinds == kinds
    assert "connection failed; retrying" in caplog.text
    assert token_file.read_text() == saved

    rig.fake.refresh_status = None  # the endpoint recovers; the backoff finds it
    await eventually(lambda: rig.broker.state(SERVER) == "connected")
    assert await asyncio.wait_for(rig.registry.dispatch(PING), 10) == "pong"
    grants = [r["grant_type"] for r in rig.fake.token_requests]
    assert grants[0] == "authorization_code"
    assert set(grants[1:]) == {"refresh_token"}  # stored login, not a new one
    assert len(rig.fake.authorize_requests) == 1
    assert rig.sent == []


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
    # The refresh token stays revoked (a working one would just reconnect, as in
    # the hiccup test below), so the way back in is a new login.
    assert await rig.broker.start_login(SERVER, SURFACE) == LINK_SENT
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
    assert (await rig.broker.complete_login(callback, SERVER)).ok
    assert await asyncio.wait_for(rig.registry.dispatch(PING), 10) == "pong"


async def test_a_login_request_that_a_refresh_can_answer_just_connects(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await park_on_a_hiccup(rig, monkeypatch)
    sent = len(rig.sent)

    assert await rig.broker.start_login(SERVER, SURFACE) == "rh connected (1 tool)"

    assert len(rig.sent) == sent  # no link: the refresh token still worked
    assert await asyncio.wait_for(rig.registry.dispatch(PING), 10) == "pong"


async def test_a_login_request_that_meets_a_struggling_endpoint_sends_no_link(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await park_on_a_hiccup(rig, monkeypatch)
    rig.fake.refresh_status = 503
    sent = len(rig.sent)

    reply = await rig.broker.start_login(SERVER, SURFACE)

    # The stored login may well still be good, so nobody is sent off to redo it.
    assert reply.startswith("login failed:") and "503" in reply
    assert len(rig.sent) == sent
    assert rig.broker.state(SERVER) == "needs_auth"
    rig.fake.refresh_status = None
    assert await rig.broker.start_login(SERVER, SURFACE) == "rh connected (1 tool)"
    assert len(rig.sent) == sent  # the stored login did it: still no link


# -- a token the server turns away early --------------------------------------


async def test_a_token_inside_its_safety_margin_is_refreshed_before_it_is_sent(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await log_in(rig)
    rig.sent.clear()
    run_the_sdk_clock_forward(monkeypatch, 3600 - 30)  # thirty seconds of life left

    assert await asyncio.wait_for(rig.registry.dispatch(PING), 10) == "pong"

    # Left to trust it to the second, the SDK would have sent it as it expired.
    grants = [r["grant_type"] for r in rig.fake.token_requests]
    assert grants == ["authorization_code", "refresh_token"]
    assert rig.broker.state(SERVER) == "connected" and rig.sent == []


@pytest.mark.parametrize("lifetime_named", [True, False], ids=["expires_in", "none"])
async def test_a_token_the_server_turns_away_early_is_refreshed_not_parked(
    rig: Rig, lifetime_named: bool
) -> None:
    # Refused before any expiry the SDK knows of (or with none at all): the SDK
    # never refreshes on a 401, it goes to the login step. The refresh token is
    # still good, so the connection tries it before anyone is asked to log in.
    rig.fake.expires_in_sent = lifetime_named
    await log_in(rig)
    rig.sent.clear()
    kinds = rig.audit_kinds
    rig.fake.revoke_access_tokens()

    # The call that found out is lost, like on any dropped connection.
    first = await asyncio.wait_for(rig.registry.dispatch(PING), 10)
    await eventually(lambda: rig.broker.state(SERVER) == "connected")

    assert first == "error: mcp server 'rh' is not connected"
    assert await asyncio.wait_for(rig.registry.dispatch(PING), 10) == "pong"
    assert rig.sent == []  # nobody was sent off to log in again
    assert rig.audit_kinds == kinds  # and no wall was recorded
    grants = [r["grant_type"] for r in rig.fake.token_requests]
    assert grants == ["authorization_code", "refresh_token"]
    assert len(rig.fake.authorize_requests) == 1


async def test_a_refresh_token_the_server_refuses_gets_one_try_and_then_parks(
    rig: Rig,
) -> None:
    await log_in(rig)
    rig.sent.clear()
    rig.fake.revoke_access_tokens()
    rig.fake.refresh_error = "invalid_grant"  # a real revocation

    reply = await asyncio.wait_for(rig.registry.dispatch(PING), 10)

    # The call in flight is held until the retry settles, so it learns the answer.
    assert reply == "error: rh needs login; call server_login"
    assert rig.broker.state(SERVER) == "needs_auth"
    assert rig.sent == [(NOTICE_TO, NOTICE)]
    grants = [r["grant_type"] for r in rig.fake.token_requests]
    assert grants == ["authorization_code", "refresh_token"]  # one try, no loop


async def test_a_call_during_the_refresh_retry_is_not_sent_down_the_old_connection(
    rig: Rig,
) -> None:
    await log_in(rig)
    rig.fake.revoke_access_tokens()
    rig.fake.refresh_delay = 0.5  # holds the retry open
    found_out = asyncio.ensure_future(rig.registry.dispatch(PING))
    await eventually(lambda: rig.broker.state(SERVER) == "connecting")

    during = await asyncio.wait_for(rig.registry.dispatch(PING), 5)

    assert during == "error: mcp server 'rh' is not connected"
    assert await asyncio.wait_for(found_out, 10) == during
    await eventually(lambda: rig.broker.state(SERVER) == "connected")
    assert await asyncio.wait_for(rig.registry.dispatch(PING), 10) == "pong"


# -- logout ------------------------------------------------------------------


async def test_logout_forgets_the_login_ends_the_connection_and_stays_quiet(
    rig: Rig,
) -> None:
    await log_in(rig)
    sent_before = list(rig.sent)

    assert await rig.broker.logout(SERVER) == "rh logged out"

    assert not (rig.root / "tokens" / "rh.json").exists()
    assert rig.broker.state(SERVER) == "needs_auth"
    assert rig.broker.needs_auth() == [SERVER]
    assert rig.fake.revocations == []  # this server advertises no endpoint
    # The tools stay registered but answer with the way back in.
    reply = await asyncio.wait_for(rig.registry.dispatch(PING), 10)
    assert reply == "error: rh needs login; call server_login"
    assert rig.audit_kinds[-1] == "mcp_auth_logout"
    # The owner just asked for this: no notice now, nor after a restart.
    again = await rig.reboot()
    try:
        assert await asyncio.wait_for(again.manager.connect(again.config), 10) == 0
    finally:
        await again.manager.stop()
    assert rig.sent == sent_before


async def test_a_logout_is_not_undone_by_the_dying_connection_refreshing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The connection ends with a DELETE through the OAuth client, and one whose
    # access token has aged out refreshes first. That refresh must not write the
    # login back: the file is all a restart would go by. This server has no
    # revocation endpoint (like Robinhood) and issues a session id, so the DELETE
    # is sent.
    await log_in(rig)
    run_the_sdk_clock_forward(monkeypatch, 7200)  # the in-memory token has run out

    assert await rig.broker.logout(SERVER) == "rh logged out"

    await eventually(lambda: ("DELETE", "/mcp") in rig.fake.requests)
    grants = [r["grant_type"] for r in rig.fake.token_requests]
    assert grants == ["authorization_code", "refresh_token"]  # it did refresh
    assert not (rig.root / "tokens" / "rh.json").exists()
    again = await rig.reboot()
    try:
        assert await asyncio.wait_for(again.manager.connect(again.config), 10) == 0
        assert again.broker.state(SERVER) == "needs_auth"  # still logged out
    finally:
        await again.manager.stop()


async def test_after_a_logout_the_owner_can_log_in_afresh(rig: Rig) -> None:
    await log_in(rig)
    await rig.broker.logout(SERVER)

    # The supervisor is parked and listening again, and re-registers the client.
    assert await rig.broker.start_login(SERVER, SURFACE) == LINK_SENT
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
    assert (await rig.broker.complete_login(callback, SERVER)).ok
    assert await asyncio.wait_for(rig.registry.dispatch(PING), 10) == "pong"
    assert len(rig.fake.registrations) == 2


async def test_logout_revokes_at_the_server_when_it_advertises_an_endpoint(
    tmp_path: Path,
) -> None:
    async with serve_fake(revocation=True) as fake:
        rig = build(fake, tmp_path, [])
        try:
            await log_in(rig)
            stored = json.loads((tmp_path / "tokens" / "rh.json").read_text())

            await rig.broker.logout(SERVER)

            assert fake.revocations == [
                {
                    "token": stored["tokens"]["refresh_token"],
                    "token_type_hint": "refresh_token",
                    "client_id": stored["client_info"]["client_id"],
                }
            ]
        finally:
            await rig.manager.stop()


async def test_a_failing_revocation_never_blocks_the_logout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with serve_fake(revocation=True, revoke_status=500) as fake:
        rig = build(fake, tmp_path, [])
        try:
            await log_in(rig)
            with caplog.at_level(logging.WARNING):
                assert await rig.broker.logout(SERVER) == "rh logged out"
        finally:
            await rig.manager.stop()
        assert len(fake.revocations) == 1
    assert not (tmp_path / "tokens" / "rh.json").exists()
    assert "revoking at" in caplog.text


async def test_logout_of_an_unknown_server_or_one_still_connecting_is_refused(
    rig: Rig,
) -> None:
    assert "not an OAuth MCP server" in await rig.broker.logout("nope")
    rig.broker.register(rig.config)
    assert "still connecting" in await rig.broker.logout(SERVER)


async def test_logging_out_while_parked_discards_the_open_link(rig: Rig) -> None:
    await connect_parked(rig)
    await rig.broker.start_login(SERVER, SURFACE)
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))

    assert await rig.broker.logout(SERVER) == "rh logged out"

    result = await rig.broker.complete_login(callback, SERVER)
    assert not result.ok and "expired or not recognized" in result.message
    assert rig.fake.token_requests == []


# -- the tools, commands and hint the owner and the model use -----------------


class Commands:
    """Stands in for ``CommandSet``: records what gets registered."""

    def __init__(self) -> None:
        self.handlers: dict[str, CommandHandler] = {}

    def register(self, name: str, handler: CommandHandler) -> None:
        self.handlers[name] = handler

    async def run(self, text: str) -> str:
        name, _, args = text[1:].partition(" ")
        channel, thread = SURFACE
        message = Message(channel=channel, sender="owner", thread_key=thread, text=text)
        return await self.handlers[name](args.strip(), message)


@dataclass
class Surfaces:
    rig: Rig
    commands: Commands
    hooks: HookRegistry

    async def call(self, name: str, **arguments: Any) -> str:
        context = ToolContext(thread_key=SURFACE[1], channel=SURFACE[0])
        call = ToolCall(id="1", name=name, arguments=arguments)
        return await self.rig.registry.dispatch(call, context)

    async def hint(self) -> str | None:
        ((_, hook),) = self.hooks.pre_turn()
        turn = TurnContext("hi", [], "owner", SURFACE[1], SURFACE[0])
        return await hook(turn)


@pytest.fixture
def surfaces(rig: Rig) -> Surfaces:
    commands, hooks = Commands(), HookRegistry()
    register_auth_surface(rig.broker, rig.registry, commands, hooks)
    return Surfaces(rig, commands, hooks)


async def test_the_login_tool_sends_the_link_to_the_calling_thread_not_the_model(
    surfaces: Surfaces,
) -> None:
    rig = surfaces.rig
    await connect_parked(rig)

    reply = await surfaces.call("server_login", server=SERVER)

    assert reply == LINK_SENT
    assert rig.sent[-1][0] == SURFACE
    assert link_in(rig.sent[-1][1]) not in reply


async def test_the_complete_tool_finishes_the_login_from_the_pasted_address(
    surfaces: Surfaces,
) -> None:
    rig = surfaces.rig
    await connect_parked(rig)
    await surfaces.call("server_login", server=SERVER)
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))

    reply = await surfaces.call(
        "server_login_complete", server=SERVER, callback_url=callback
    )

    assert reply == "rh connected (1 tool)"
    assert await surfaces.call("mcp_rh_ping") == "pong"


async def test_the_login_tools_report_bad_requests_as_strings(
    surfaces: Surfaces,
) -> None:
    assert "not an OAuth MCP server" in await surfaces.call("server_login", server="x")
    done = await surfaces.call("server_login_complete", server="x", callback_url="junk")
    assert "expired or not recognized" in done


async def test_the_login_tool_needs_to_know_which_conversation_asked(rig: Rig) -> None:
    register_auth_surface(rig.broker, rig.registry, Commands(), HookRegistry())
    call = ToolCall(id="1", name="server_login", arguments={"server": SERVER})
    assert "session context" in await rig.registry.dispatch(call)


async def test_login_command_starts_a_login_and_finishes_it_with_the_address(
    surfaces: Surfaces,
) -> None:
    rig = surfaces.rig
    await connect_parked(rig)

    assert await surfaces.commands.run("/login rh") == LINK_SENT
    assert rig.sent[-1][0] == SURFACE
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))

    reply = await surfaces.commands.run(f"/login rh {callback}")
    assert reply == "rh connected (1 tool)"


async def test_login_and_logout_commands_explain_bad_arguments(
    surfaces: Surfaces,
) -> None:
    run = surfaces.commands.run
    for bad in ("/login", "/login a b c"):
        assert (await run(bad)).startswith("usage: /login")
    assert await run("/logout") == "usage: /logout <server>"
    assert await run("/logout a b") == "usage: /logout <server>"
    assert "not an OAuth MCP server" in await run("/login nope")
    assert "not an OAuth MCP server" in await run("/logout nope")


async def test_logout_command_forgets_the_login(surfaces: Surfaces) -> None:
    await log_in(surfaces.rig)
    assert await surfaces.commands.run("/logout rh") == "rh logged out"
    assert not (surfaces.rig.root / "tokens" / "rh.json").exists()


async def test_the_model_is_told_which_servers_are_waiting_for_a_login(
    surfaces: Surfaces,
) -> None:
    assert await surfaces.hint() is None  # nothing needs a login yet
    await connect_parked(surfaces.rig)
    assert await surfaces.hint() == (
        "MCP servers needing login: rh "
        "(call server_login when the owner asks to use one)"
    )
    await log_in_from_parked(surfaces.rig)
    assert await surfaces.hint() is None


async def log_in_from_parked(rig: Rig) -> None:
    await rig.broker.start_login(SERVER, SURFACE)
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
    assert (await rig.broker.complete_login(callback, SERVER)).ok


# -- the web callback ----------------------------------------------------------


def web_client(rig: Rig) -> httpx.AsyncClient:
    app = Starlette(routes=build_oauth_routes(rig.broker))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8130"
    )


def local_path(callback: str) -> str:
    """The path+query of the address the browser landed on."""
    return callback.removeprefix("http://127.0.0.1:8130")


async def test_the_callback_route_completes_a_login_with_no_password(rig: Rig) -> None:
    await connect_parked(rig)
    await rig.broker.start_login(SERVER, SURFACE)
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))
    code = callback.split("code=")[1].split("&")[0]

    async with web_client(rig) as client:  # no cookie: the browser just arrives
        response = await client.get(local_path(callback))

    assert response.status_code == 200
    assert "rh connected (1 tool)" in response.text
    assert "you can close this tab" in response.text
    assert code not in response.text  # never echoed back
    assert rig.broker.state(SERVER) == "connected"
    assert await rig.registry.dispatch(PING) == "pong"


async def test_the_callback_route_refuses_a_state_it_did_not_issue(rig: Rig) -> None:
    await connect_parked(rig)
    await rig.broker.start_login(SERVER, SURFACE)

    async with web_client(rig) as client:
        forged = await client.get("/oauth/callback?code=evil&state=guess")
        bare = await client.get("/oauth/callback")

    assert forged.status_code == bare.status_code == 400
    assert "expired or not recognized" in forged.text
    assert rig.fake.token_requests == []
    assert rig.broker.state(SERVER) == "needs_auth"


async def test_the_callback_route_reports_a_declined_login(rig: Rig) -> None:
    rig.fake.deny_authorize = True
    await connect_parked(rig)
    await rig.broker.start_login(SERVER, SURFACE)
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))

    async with web_client(rig) as client:
        response = await client.get(local_path(callback))

    assert response.status_code == 400
    assert "login denied (access_denied)" in response.text


async def test_a_prefetch_of_the_callback_does_not_spend_the_login(rig: Rig) -> None:
    await connect_parked(rig)
    await rig.broker.start_login(SERVER, SURFACE)
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))

    async with web_client(rig) as client:
        assert (await client.head(local_path(callback))).status_code == 405
        assert (await client.get(local_path(callback))).status_code == 200


async def test_a_link_answered_twice_connects_once(rig: Rig) -> None:
    await connect_parked(rig)
    await rig.broker.start_login(SERVER, SURFACE)
    callback = await rig.fake.browser_approve(link_in(rig.sent[-1][1]))

    async with web_client(rig) as client:
        first = await client.get(local_path(callback))
        second = await client.get(local_path(callback))

    assert (first.status_code, second.status_code) == (200, 400)
    assert [r["grant_type"] for r in rig.fake.token_requests] == ["authorization_code"]
