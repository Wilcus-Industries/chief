"""MCP OAuth end to end against a local fake authorization + resource server.

Everything real except the far end: the SDK's own OAuth provider, a live
supervisor over a real streamable-HTTP transport, the token file on disk, the
broker's notices and audit trail. The fake (tests/fake_oauth.py) stands in for
the provider and "the owner's browser" is ``FakeOAuth.browser_approve``.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from chief.audit import AuditLog
from chief.mcpclient.manager import McpManager, ServerConfig
from chief.mcpclient.oauth import OAuthBroker
from chief.mcpclient.oauth_types import Surface
from chief.tools import ToolRegistry

from .fake_oauth import FakeOAuth, serve_fake

SERVER = "rh"
REDIRECT = "http://127.0.0.1:8130/oauth/callback"
NOTICE_TO: Surface = ("imessage", "+15550001111")
NOTICE = 'Rh needs login. Say "connect rh" or /login rh.'


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

    @property
    def audit_kinds(self) -> list[str]:
        path = self.root / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["kind"] for line in path.read_text().splitlines()]

    async def reboot(self) -> "Rig":
        await self.manager.stop()
        return build(self.fake, self.root, self.sent)


def build(fake: FakeOAuth, root: Path, sent: list[tuple[Surface, str]]) -> Rig:
    async def send(surface: Surface, text: str) -> None:
        sent.append((surface, text))

    broker = OAuthBroker(
        send=send,
        audit=AuditLog(root / "audit.jsonl"),
        notice_to=NOTICE_TO,
        redirect_uri=REDIRECT,
        token_dir=root / "tokens",
        notified_path=root / "notified.json",
    )
    registry = ToolRegistry()
    config = ServerConfig(name=SERVER, url=fake.mcp_url, auth="oauth")
    return Rig(fake, root, sent, broker, McpManager(registry, broker), registry, config)


@pytest.fixture
async def fake() -> AsyncIterator[FakeOAuth]:
    async with serve_fake() as server:
        yield server


@pytest.fixture
async def rig(tmp_path: Path, fake: FakeOAuth) -> AsyncIterator[Rig]:
    current = build(fake, tmp_path, [])
    yield current
    await current.manager.stop()


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
