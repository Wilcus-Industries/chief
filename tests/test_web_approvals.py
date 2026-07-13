"""Web approvals integration tests (#153): real cards, real registry, real HTTP.

The gate stack is real (PolicyStore + ApprovalManager + Blacklist + shared
ApprovalRegistry); ``_GateSession`` drives the captured ``can_use_tool`` with a
blacklisted command, so classify → ASK → card → answer runs the true central
mechanism end to end — the browser answers through POST and watches SSE.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.mirror import MirrorTaskIO
from chief.gate.approvals import ApprovalManager
from chief.gate.policy import PolicyStore
from chief.obs.audit import AuditLog
from chief.persistence.messages import MessageLog
from test_broadcast_bus import _RecordingInner
from test_cli_platform import _gate_factory
from web_helpers import SseReader, WebStack, start_web_stack


@pytest.fixture
async def gate_stack(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[WebStack]:
    stack = await start_web_stack(
        tmp_path, session_factory, sdk_factory=_gate_factory(), with_gate=True
    )
    try:
        yield stack
    finally:
        await stack.aclose()


def _card_id(html: str) -> int:
    match = re.search(r"card-(\d+)", html)
    assert match, f"no card id in fragment: {html!r}"
    return int(match.group(1))


async def test_card_streams_shows_on_page_and_approve_lets_it_proceed(
    gate_stack: WebStack,
) -> None:
    async with SseReader(
        gate_stack.client, "/events?platform=cli&thread_key=cli:main"
    ) as sse:
        resp = await gate_stack.client.post(
            "/chat/send",
            data={"platform": "cli", "thread_key": "cli:main", "text": "go"},
        )
        assert resp.status_code == 200

        event, data = await sse.next_event()
        assert event == "approvals"
        assert "sudo rm -rf /" in data
        approval_id = _card_id(data)
        assert "approve_once" in data and "always_deny" in data

        # The page render shows the same pending card (bridge-tracked state).
        page = await gate_stack.client.get("/approvals")
        assert f"card-{approval_id}" in page.text

        resp = await gate_stack.client.post(
            f"/approvals/{approval_id}", data={"action": "approve_once"}
        )
        assert resp.status_code == 200
        assert f"card-{approval_id}" not in resp.text  # optimistically cleared

        seen: list[str] = []
        for _ in range(3):
            event, data = await sse.next_event()
            seen.append(f"{event}:{data}")
        blob = "\n".join(seen)
        assert "proceeded" in blob  # the gated command really ran
        # The approvals block re-rendered empty once the card resolved.
        assert any(s.startswith("approvals:") for s in seen)


async def test_deny_blocks_the_command(gate_stack: WebStack) -> None:
    async with SseReader(
        gate_stack.client, "/events?platform=cli&thread_key=cli:main"
    ) as sse:
        await gate_stack.client.post(
            "/chat/send",
            data={"platform": "cli", "thread_key": "cli:main", "text": "go"},
        )
        event, data = await sse.next_event()
        approval_id = _card_id(data)

        resp = await gate_stack.client.post(
            f"/approvals/{approval_id}", data={"action": "deny_once"}
        )
        assert resp.status_code == 200

        seen = []
        for _ in range(3):
            event, data = await sse.next_event()
            seen.append(data)
        assert any("blocked" in s for s in seen)


async def test_second_answer_is_conflict(gate_stack: WebStack) -> None:
    async with SseReader(
        gate_stack.client, "/events?platform=cli&thread_key=cli:main"
    ) as sse:
        await gate_stack.client.post(
            "/chat/send",
            data={"platform": "cli", "thread_key": "cli:main", "text": "go"},
        )
        _, data = await sse.next_event()
        approval_id = _card_id(data)

        first = await gate_stack.client.post(
            f"/approvals/{approval_id}", data={"action": "approve_once"}
        )
        assert first.status_code == 200
        # Wait for resolution so the card is gone from the bridge's pending set.
        for _ in range(3):
            await sse.next_event()

    second = await gate_stack.client.post(
        f"/approvals/{approval_id}", data={"action": "deny_once"}
    )
    assert second.status_code == 409


async def test_unknown_action_is_rejected(gate_stack: WebStack) -> None:
    resp = await gate_stack.client.post("/approvals/1", data={"action": "explode"})
    assert resp.status_code == 400


async def test_card_raised_on_a_chat_platform_is_answerable_from_the_browser(
    gate_stack: WebStack,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A telegram-stack approval (real manager over a real mirror, same shared
    # registry) fans onto the socket, renders as a web card, and the browser's
    # answer resolves the parked request — cross-surface parity (#136).
    assert gate_stack.registry is not None
    inner = _RecordingInner()
    audit = AuditLog(str(tmp_path / "tg-audit.jsonl"))
    policy = PolicyStore(session_factory, audit=audit)
    await policy.load()
    mirror = MirrorTaskIO(
        inner,
        platform="telegram",
        log=MessageLog(session_factory),
        server=gate_stack.server,
    )
    manager = ApprovalManager(
        session_factory=session_factory,
        io=mirror,
        policy=policy,
        audit=audit,
        registry=gate_stack.registry,
        timeout_seconds=5.0,
    )

    async with SseReader(
        gate_stack.client, "/events?platform=cli&thread_key=cli:main"
    ) as sse:
        parked = asyncio.create_task(
            manager.request(
                task_id=None,
                thread_key="telegram:room1",
                tier="owner",
                tool_name="Bash",
                tool_input={"command": "git push"},
                route="telegram:room1",
            )
        )
        event, data = await sse.next_event()
        assert event == "approvals"
        assert "telegram" in data
        approval_id = _card_id(data)

        resp = await gate_stack.client.post(
            f"/approvals/{approval_id}", data={"action": "approve_once"}
        )
        assert resp.status_code == 200
        assert await asyncio.wait_for(parked, 5) is True
