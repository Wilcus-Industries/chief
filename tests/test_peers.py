"""Session-to-session messaging through the real wiring (PRD #308).

The central mechanism, exercised for real: a ``send_msg`` from one live
``build_app`` session lands in another — injected at its next tool step if it
is busy, starting a turn if it is idle — and the receiver's output follows the
owner/non-owner rule. Only the model (FakeProvider routes, one per session)
and the device transports (capturing adapters) are faked.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from chief.adapters.base import Message
from chief.app import App
from chief.peers import LOOP_WARNING
from chief.provider.base import Completion, ToolCall

from .fakes import CapturingAdapter, FakeProvider, text_turn, tool_turn
from .test_app import boot, make_config, shutdown

OWNER_HANDLE = "+15550000001"
STRANGER = "+15559990000"
Frame = dict[str, Any]


@asynccontextmanager
async def running(
    tmp_path: Path, sock_path: Path, provider: FakeProvider, **overrides: Any
) -> AsyncIterator[tuple[App, CapturingAdapter, CapturingAdapter]]:
    config = make_config(
        tmp_path, sock_path, imessage_owner_handles=(OWNER_HANDLE,),
        gate_approved=("send_msg", "read_file", "spawn_agent"), **overrides,
    )
    app, streams = await boot(config, provider)
    device = CapturingAdapter("imessage")
    web = CapturingAdapter("web")
    app.dispatcher.register(device)
    app.dispatcher.register(web)
    for thread, channel in (
        ("web:main", "web"), (OWNER_HANDLE, "imessage"), (STRANGER, "imessage"),
        ("cli:a", "cli"), ("cli:b", "cli"),
    ):
        await app.store.ensure_session(thread, channel)
    try:
        yield app, device, web
    finally:
        await shutdown(app, streams)


class Ticks:
    """Counts completed turns per thread off the hub's coarse ticks."""

    def __init__(self, app: App) -> None:
        self._queue = app.hub.listen()
        self.seen: dict[str, int] = {}

    async def wait(self, thread: str, count: int = 1) -> None:
        async def until() -> None:
            while self.seen.get(thread, 0) < count:
                frame = await self._queue.get()
                if frame["type"] == "tick":
                    self.seen[frame["thread"]] = self.seen.get(frame["thread"], 0) + 1

        await asyncio.wait_for(until(), timeout=5)


def said(adapter: CapturingAdapter) -> list[tuple[str, str]]:
    """Outbound sends minus the gate's "⚙ tool" announcements."""
    return [(t, text) for t, text in adapter.sent if not text.startswith("⚙")]


async def user_texts(app: App, thread: str) -> list[str]:
    return [str(m["content"]) for m in await app.store.load(thread)
            if m["role"] == "user"]


def owner(channel: str, thread: str, text: str) -> Message:
    return Message(channel=channel, sender="owner", thread_key=thread, text=text)


async def test_cross_channel_relay_round_trip(tmp_path: Path, sock_path: Path) -> None:
    provider = FakeProvider([
        tool_turn("send_msg", {"to": OWNER_HANDLE, "text": "text the owner: 7pm"}),
        text_turn("asked imessage"),
    ])
    # The web thread's wake, once the iMessage thread answers.
    provider.route(f"from session {OWNER_HANDLE}", [text_turn("imessage confirmed")])
    # The iMessage thread's turn: confirm back to web, then text the owner.
    provider.route("from session web:main", [
        tool_turn("send_msg", {"to": "web:main", "text": "texted them"}),
        text_turn("Dinner is at 7pm"),
    ])
    async with running(tmp_path, sock_path, provider) as (app, device, web):
        ticks = Ticks(app)
        await app.dispatcher.handle(owner("web", "web:main", "text me 7pm on imessage"))
        await ticks.wait(OWNER_HANDLE)
        await ticks.wait("web:main", 2)
        assert said(device) == [(OWNER_HANDLE, "Dinner is at 7pm")]
        assert said(web) == [
            ("web:main", "asked imessage"), ("web:main", "imessage confirmed"),
        ]
        relayed = (await user_texts(app, OWNER_HANDLE))[0]
        assert relayed.startswith("[message m-")
        assert "from session web:main]\ntext the owner: 7pm" in relayed
        reply = (await user_texts(app, "web:main"))[-1]
        assert f"from session {OWNER_HANDLE}]\ntexted them" in reply


async def test_message_to_a_busy_session_lands_at_its_next_tool_step(
    tmp_path: Path, sock_path: Path
) -> None:
    hold = asyncio.Event()
    provider = FakeProvider([])
    provider.route("RECEIVER-WORK", [
        tool_turn("read_file", {"path": "README.md"}), text_turn("done working"),
    ], hold=hold)
    provider.route("SENDER-GO", [
        tool_turn("send_msg", {"to": "cli:b", "text": "also check the logs"}),
        text_turn("told b"),
    ])
    async with running(tmp_path, sock_path, provider) as (app, _device, _web):
        ticks = Ticks(app)
        receiver = asyncio.create_task(
            app.dispatcher.handle(owner("cli", "cli:b", "RECEIVER-WORK"))
        )
        while not any("RECEIVER-WORK" in json.dumps(c) for c in provider.calls):
            await asyncio.sleep(0)
        await app.dispatcher.handle(owner("cli", "cli:a", "SENDER-GO"))
        hold.set()
        await receiver
        await ticks.wait("cli:b")
        # The receiver's second model call — inside the same turn, before its
        # final answer — carries the injected message after the tool result.
        second = [c for c in provider.calls if "RECEIVER-WORK" in json.dumps(c)][1]
        assert second[-2]["role"] == "tool"
        assert second[-1]["role"] == "user"
        assert "from session cli:a]\nalso check the logs" in second[-1]["content"]
        assert ticks.seen.get("cli:b") == 1  # one turn, not a second wake


async def test_message_to_an_idle_session_starts_a_turn(
    tmp_path: Path, sock_path: Path
) -> None:
    provider = FakeProvider([
        tool_turn("send_msg", {"to": "cli:b", "text": "ping", "reply_to": "m-0"}),
        text_turn("sent"),
    ])
    provider.route("from session cli:a", [text_turn("pong")])
    async with running(tmp_path, sock_path, provider) as (app, _device, _web):
        ticks = Ticks(app)
        await app.dispatcher.handle(owner("cli", "cli:a", "ping b"))
        await ticks.wait("cli:b")
        [received] = await user_texts(app, "cli:b")
        assert " re m-0]\nping" in received
        audit = [json.loads(line) for line in
                 (tmp_path / "audit.jsonl").read_text().splitlines()]
        [entry] = [e for e in audit if e["kind"] == "peer_message"]
        assert entry["sender"] == "cli:a" and entry["to"] == "cli:b"
        assert entry["reply_to"] == "m-0" and entry["id"].startswith("m-")
        assert "text" not in entry  # who and when, never the content


async def test_send_scope_errors(tmp_path: Path, sock_path: Path) -> None:
    calls = tuple(
        ToolCall(id=f"c{i}", name="send_msg", arguments={"to": to, "text": "x"})
        for i, to in enumerate(["cli:a", "cli:nowhere", "sub:cli:b:abc123"])
    )
    provider = FakeProvider([[Completion(text="", tool_calls=calls)], text_turn("ok")])
    async with running(tmp_path, sock_path, provider) as (app, _device, _web):
        await app.dispatcher.handle(owner("cli", "cli:a", "try"))
        results = [m["content"] for m in provider.calls[1] if m["role"] == "tool"]
        assert results[0].startswith("error: you can't send a message to yourself")
        assert results[1].startswith("error: no such session 'cli:nowhere'")
        assert results[2].startswith("error: no live subagent of yours")


async def test_loop_warning_reaches_both_sides_and_still_delivers(
    tmp_path: Path, sock_path: Path
) -> None:
    calls = tuple(
        ToolCall(id=f"c{i}", name="send_msg", arguments={"to": "cli:b", "text": t})
        for i, t in enumerate(["one", "two"])
    )
    provider = FakeProvider([[Completion(text="", tool_calls=calls)], text_turn("ok")])
    provider.route("from session cli:a", [text_turn("b1"), text_turn("b2")])
    async with running(
        tmp_path, sock_path, provider, peers_loop_warn_count=2
    ) as (app, _device, _web):
        ticks = Ticks(app)
        await app.dispatcher.handle(owner("cli", "cli:a", "spam b"))
        await ticks.wait("cli:b", 2)
        results = [m["content"] for m in provider.calls[1] if m["role"] == "tool"]
        assert LOOP_WARNING not in results[0]
        assert results[1].startswith("sent m-") and LOOP_WARNING in results[1]
        received = "\n".join(await user_texts(app, "cli:b"))
        assert "\none" in received and "\ntwo" in received  # both delivered
        assert received.count(LOOP_WARNING) == 1


async def test_peer_turn_in_a_stranger_thread_is_quiet(
    tmp_path: Path, sock_path: Path
) -> None:
    provider = FakeProvider([
        tool_turn("send_msg", {"to": STRANGER, "text": "tell them hi"}),
        text_turn("sent"),
    ])
    provider.route("from session cli:a", [text_turn("hi stranger")])
    async with running(tmp_path, sock_path, provider) as (app, device, _web):
        ticks = Ticks(app)
        await app.dispatcher.handle(owner("cli", "cli:a", "go"))
        await ticks.wait(STRANGER)
        assert device.sent == []
        stored = await app.store.load(STRANGER)
        assert stored[-1] == {"role": "assistant", "content": "hi stranger"}
