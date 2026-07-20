"""Whole-daemon integration through build_app: the production wiring with
only the LLM provider swapped (PRD #183 testing seam)."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from chief.agent.tools import ToolRegistry
from chief.app import App, build_app
from chief.bus import Event
from chief.config import AliasSpec, BackendSpec, Config
from chief.mcpclient.manager import ServerConfig
from chief.provider.base import Completion, ToolCall
from chief.provider.openrouter import OpenRouterProvider
from chief.provider.router import RouterProvider
from chief.shelltool import GUARD_REFUSED_EXIT_CODE
from chief.wiring import build_mcp, build_provider

from .fakes import FakeProvider, text_turn

Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]


def make_config(tmp_path: Path, sock: Path, **overrides: Any) -> Config:
    return Config(
        models={"default": "test-model"},
        db_path=tmp_path / "chief.db",
        socket_path=sock,
        **overrides,
    )


async def boot(config: Config, provider: FakeProvider) -> tuple[App, Streams]:
    app = await build_app(config, provider=provider)
    await app.start()
    streams = await asyncio.open_unix_connection(str(config.socket_path))
    return app, streams


async def shutdown(app: App, streams: Streams) -> None:
    streams[1].close()
    await app.stop()


def send_frame(streams: Streams, text: str, thread: str = "t1") -> None:
    streams[1].write(json.dumps({"thread": thread, "text": text}).encode() + b"\n")


async def read_finals(
    streams: Streams, count: int, *, announcements: bool = False
) -> list[dict[str, Any]]:
    """Read ``count`` final frames. Gate tool-call announcements ("⚙ …") are
    skipped unless asked for — one precedes every non-card gated call."""
    finals: list[dict[str, Any]] = []
    while len(finals) < count:
        line = await asyncio.wait_for(streams[0].readline(), timeout=5)
        assert line, "connection closed early"
        frame = json.loads(line)
        if frame["type"] != "final":
            continue
        if announcements or not frame["text"].startswith("⚙"):
            finals.append(frame)
    return finals


async def test_boot_answers_a_turn_when_no_packages_declare_hooks(
    tmp_path: Path, sock_path: Path
) -> None:
    # Regression: the hooks load phase must never block boot. With nothing
    # installed, load_hooks is a no-op and the daemon answers normally.
    provider = FakeProvider([text_turn("hello from chief")])
    app, streams = await boot(make_config(tmp_path, sock_path), provider)
    try:
        send_frame(streams, "hi", thread="t1")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "hello from chief"
    finally:
        await shutdown(app, streams)


def test_build_provider_empty_config_is_plain_openrouter() -> None:
    provider = build_provider(Config(openrouter_api_key="k"))
    assert isinstance(provider, OpenRouterProvider)


def test_build_provider_assembles_a_router_when_backends_configured() -> None:
    config = Config(
        provider_backends={
            "proxy": BackendSpec(base_url="http://127.0.0.1:8000/v1", api_key="x")
        },
        provider_aliases={"opus": AliasSpec(backend="proxy", model="claude-opus-4-8")},
    )
    assert isinstance(build_provider(config), RouterProvider)


def test_build_provider_rejects_an_alias_to_an_unknown_backend() -> None:
    config = Config(
        provider_backends={
            "proxy": BackendSpec(base_url="http://127.0.0.1:8000/v1", api_key="x")
        },
        provider_aliases={"opus": AliasSpec(backend="typo", model="m")},
    )
    with pytest.raises(ValueError, match="opus.*typo"):
        build_provider(config)


def test_build_mcp_translates_yaml_entries_to_server_configs() -> None:
    config = Config(
        mcp_servers={
            "http_server": {"url": "http://127.0.0.1:9000"},
            "stdio_server": {
                "command": ["python", "server.py"],
                "env": {"API_KEY": "secret"},
                "cwd": "/srv/mcp",
            },
            "bare_stdio": {"command": ["mcp-tool"]},
        }
    )
    _, mcp_configs = build_mcp(config, ToolRegistry())
    by_name = {sc.name: sc for sc in mcp_configs}

    assert by_name["http_server"] == ServerConfig(
        name="http_server", url="http://127.0.0.1:9000"
    )
    assert by_name["stdio_server"] == ServerConfig(
        name="stdio_server",
        command=("python", "server.py"),
        env={"API_KEY": "secret"},
        cwd="/srv/mcp",
    )
    # No env/cwd declared: both stay None, not empty collections.
    assert by_name["bare_stdio"] == ServerConfig(
        name="bare_stdio", command=("mcp-tool",)
    )


def test_build_mcp_empty_config_yields_no_servers() -> None:
    _, mcp_configs = build_mcp(Config(), ToolRegistry())
    assert mcp_configs == ()


async def test_switch_model_is_gated_and_persists_the_override(
    tmp_path: Path, sock_path: Path
) -> None:
    switch = ToolCall(id="c1", name="switch_model", arguments={"model": "opus"})
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(switch,))], text_turn("switched")]
    )
    # `opus` has to be a configured alias to be accepted — a bare name that
    # routes nowhere is rejected before it can wedge the thread.
    config = make_config(
        tmp_path,
        sock_path,
        provider_aliases={"opus": AliasSpec(backend="proxy", model="claude-opus-4-8")},
    )
    app, streams = await boot(config, provider)
    try:
        send_frame(streams, "use opus please", thread="t1")
        card = (await read_finals(streams, 1))[0]
        assert "approve tool call switch_model" in card["text"]
        send_frame(streams, "yes", thread="t1")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "switched"
        assert await app.store.model_override("cli:t1") == "opus"
    finally:
        await shutdown(app, streams)


async def test_agent_creates_a_monitor_and_it_fires(
    tmp_path: Path, sock_path: Path
) -> None:
    create = ToolCall(
        id="c1",
        name="monitor",
        arguments={
            "action": "create",
            "description": "urgent watcher",
            "pattern": "urgent",
        },
    )
    provider = FakeProvider(
        [
            [Completion(text="", tool_calls=(create,))],
            text_turn("watching for urgent messages"),
            text_turn("monitor woke me, on it"),
        ]
    )
    config = make_config(tmp_path, sock_path, gate_approved=("monitor",))
    app, streams = await boot(config, provider)
    try:
        send_frame(streams, "watch this channel for urgent stuff", thread="t1")
        first = await read_finals(streams, 1)
        assert first[0]["text"] == "watching for urgent messages"
        monitors = await app.monitor_service.list_enabled()
        assert len(monitors) == 1
        assert monitors[0].wake_thread == "cli:t1"

        # A stranger's urgent message on the watched channel wakes t1 through
        # the monitor. (Owner messages already dispatch, so monitors skip them.)
        await app.bus.publish(
            Event(
                type="message.inbound",
                channel="cli",
                payload={
                    "thread_key": "cli:stranger",
                    "sender": "+15559998888",
                    "text": "URGENT: the roof is leaking",
                },
            )
        )
        final = (await read_finals(streams, 1))[0]
        assert final["thread"] == "cli:t1"
        assert final["text"] == "monitor woke me, on it"
    finally:
        await shutdown(app, streams)


async def test_agent_reads_a_bundled_package_manifest(
    tmp_path: Path, sock_path: Path
) -> None:
    # Discovery is read-tool driven now: read_file is read_only, so it needs
    # no approval, and it reaches the bundled manifests under packages/.
    call = ToolCall(
        id="c1",
        name="read_file",
        arguments={"path": "packages/build-imessage/manifest.yaml"},
    )
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(call,))], text_turn("read the manifest")]
    )
    app, streams = await boot(make_config(tmp_path, sock_path), provider)
    try:
        send_frame(streams, "what does build-imessage need?", thread="t1")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "read the manifest"
        result = provider.calls[1][-1]
        assert result["role"] == "tool"
        assert "build-imessage" in result["content"]
        assert "screening" in result["content"]
    finally:
        await shutdown(app, streams)


async def test_uncarded_tool_call_is_announced_on_the_channel(
    tmp_path: Path, sock_path: Path
) -> None:
    """A call that needs no approval still shows up on the owner's surface."""
    call = ToolCall(
        id="c1",
        name="read_file",
        arguments={"path": "packages/build-imessage/manifest.yaml"},
    )
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(call,))], text_turn("done")]
    )
    app, streams = await boot(make_config(tmp_path, sock_path), provider)
    try:
        send_frame(streams, "read it", thread="t1")
        frames = await read_finals(streams, 2, announcements=True)
        assert frames[0]["thread"] == "cli:t1"
        assert frames[0]["text"].startswith("⚙ read_file")
        assert "manifest.yaml" in frames[0]["text"]
        assert frames[1]["text"] == "done"
    finally:
        await shutdown(app, streams)


async def test_announcements_are_off_when_configured(
    tmp_path: Path, sock_path: Path
) -> None:
    call = ToolCall(
        id="c1",
        name="read_file",
        arguments={"path": "packages/build-imessage/manifest.yaml"},
    )
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(call,))], text_turn("done")]
    )
    config = make_config(tmp_path, sock_path, gate_announce=False)
    app, streams = await boot(config, provider)
    try:
        send_frame(streams, "read it", thread="t1")
        frames = await read_finals(streams, 1, announcements=True)
        assert frames[0]["text"] == "done"
    finally:
        await shutdown(app, streams)


async def test_gray_tool_raises_an_approval_card_first_answer_wins(
    tmp_path: Path, sock_path: Path
) -> None:
    create = ToolCall(
        id="c1",
        name="schedule",
        arguments={
            "action": "create",
            "description": "daily checkin",
            "spec": "0 9 * * *",
            "prompt": "say hi",
        },
    )
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(create,))], text_turn("scheduled")]
    )
    app, streams = await boot(make_config(tmp_path, sock_path), provider)
    try:
        send_frame(streams, "remind me daily", thread="t1")
        card = (await read_finals(streams, 1))[0]
        assert "approve tool call schedule" in card["text"]
        send_frame(streams, "yes", thread="t1")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "scheduled"
        schedules = await app.cron_service.list_enabled()
        assert len(schedules) == 1
        assert schedules[0].spec == "0 9 * * *"
    finally:
        await shutdown(app, streams)


async def test_denied_card_blocks_the_tool(
    tmp_path: Path, sock_path: Path
) -> None:
    create = ToolCall(
        id="c1",
        name="schedule",
        arguments={
            "action": "create",
            "description": "d",
            "spec": "@every 60",
            "prompt": "p",
        },
    )
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(create,))], text_turn("understood, denied")]
    )
    app, streams = await boot(make_config(tmp_path, sock_path), provider)
    try:
        send_frame(streams, "remind me", thread="t1")
        await read_finals(streams, 1)  # the card
        send_frame(streams, "no", thread="t1")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "understood, denied"
        assert await app.cron_service.list_enabled() == []
        # The model was told the gate denied it.
        denied = provider.calls[1][-1]
        assert denied["role"] == "tool"
        assert "denied by the gate" in denied["content"]
    finally:
        await shutdown(app, streams)


def command_create(command: str) -> ToolCall:
    return ToolCall(
        id="c1",
        name="schedule",
        arguments={
            "action": "create",
            "description": "prune the cache",
            "spec": "0 4 * * *",
            "command": command,
        },
    )


async def test_scheduled_command_creation_raises_a_card(
    tmp_path: Path, sock_path: Path
) -> None:
    # gate_approved=("schedule",) is load-bearing: the gate raises no card for
    # this call, so any card that appears came from the tool itself.
    command = "rm -rf /tmp/chief-cache"
    provider = FakeProvider(
        [
            [Completion(text="", tool_calls=(command_create(command),))],
            text_turn("scheduled"),
        ]
    )
    config = make_config(tmp_path, sock_path, gate_approved=("schedule",))
    app, streams = await boot(config, provider)
    try:
        send_frame(streams, "prune the cache nightly", thread="t1")
        card = (await read_finals(streams, 1))[0]
        assert command in card["text"]
        send_frame(streams, "yes", thread="t1")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "scheduled"
        schedules = await app.cron_service.list_enabled()
        assert len(schedules) == 1
        assert schedules[0].command == command
    finally:
        await shutdown(app, streams)


async def test_declined_scheduled_command_creates_no_row(
    tmp_path: Path, sock_path: Path
) -> None:
    provider = FakeProvider(
        [
            [Completion(text="", tool_calls=(command_create("rm -rf /tmp/x"),))],
            text_turn("understood"),
        ]
    )
    config = make_config(tmp_path, sock_path, gate_approved=("schedule",))
    app, streams = await boot(config, provider)
    try:
        send_frame(streams, "prune the cache nightly", thread="t1")
        await read_finals(streams, 1)  # the tool's card
        send_frame(streams, "no", thread="t1")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "understood"
        assert await app.cron_service.list_enabled() == []
        result = provider.calls[1][-1]
        assert result["role"] == "tool"
        assert "not created" in result["content"]
    finally:
        await shutdown(app, streams)


async def test_prompt_schedule_creation_raises_no_card(
    tmp_path: Path, sock_path: Path
) -> None:
    create = ToolCall(
        id="c1",
        name="schedule",
        arguments={
            "action": "create",
            "description": "daily checkin",
            "spec": "0 9 * * *",
            "prompt": "say hi",
        },
    )
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(create,))], text_turn("scheduled")]
    )
    config = make_config(tmp_path, sock_path, gate_approved=("schedule",))
    app, streams = await boot(config, provider)
    try:
        send_frame(streams, "remind me daily", thread="t1")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "scheduled"
        assert len(await app.cron_service.list_enabled()) == 1
    finally:
        await shutdown(app, streams)


async def test_scheduled_command_gets_the_owner_send_guard(
    tmp_path: Path, sock_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The shell tool's echo-loop seatbelt must also cover cron's unattended
    # runner — an approved schedule is otherwise a way around it, with nobody
    # present to notice the loop start.
    config = make_config(
        tmp_path, sock_path, imessage_owner_handles=("+15551234567",)
    )
    app = await build_app(config, provider=FakeProvider([]))
    marker = tmp_path / "sent.txt"
    # Created before start(): the loop sleeps its full poll when it boots empty.
    await app.cron_service.create(
        description="page the owner",
        spec="@every 0.05",
        wake_channel="cli",
        wake_thread="cli:home",
        command=f"echo sent > {marker}; imsg send --to +15551234567 hi",
    )
    with caplog.at_level(logging.INFO, logger="chief.cron.service"):
        await app.start()
        streams = await asyncio.open_unix_connection(str(config.socket_path))
        try:
            for _ in range(200):
                if any("command exit=" in r.getMessage() for r in caplog.records):
                    break
                await asyncio.sleep(0.02)
        finally:
            await shutdown(app, streams)
    fired = [r.getMessage() for r in caplog.records if "command exit=" in r.message]
    assert fired, "the command schedule never fired"
    # Refused by the guard before reaching the shell — so `echo` never ran.
    assert f"exit={GUARD_REFUSED_EXIT_CODE}" in fired[0]
    assert not marker.exists()
