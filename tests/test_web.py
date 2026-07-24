"""Web UI: auth, chat send, SSE frames, monitor list."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Message
from chief.agent.manager import SessionManager
from chief.bus import EventBus
from chief.classifiers import Classifier, ClassifierRegistry
from chief.hub import ObserverHub
from chief.monitors.service import MonitorService
from chief.persistence.db import make_session_factory
from chief.persistence.store import MessageStore
from chief.tools import ToolRegistry
from chief.web.adapter import WebAdapter
from chief.web.app import build_web_app
from chief.web.auth import Auth

from .fakes import FakeProvider

HANDLED: list[Message] = []


async def handle(message: Message) -> None:
    HANDLED.append(message)


async def _no_wake(message: Message) -> None:
    raise AssertionError("unexpected wake")


def _palette() -> list[str]:
    return ["/help", "/model", "/monitors"]


WebParts = tuple[
    httpx.AsyncClient,
    WebAdapter,
    MonitorService,
    MessageStore,
    SessionManager,
    ObserverHub,
]


@pytest.fixture
def web(engine: AsyncEngine) -> WebParts:
    HANDLED.clear()
    hub = ObserverHub()
    adapter = WebAdapter(hub)
    factory = make_session_factory(engine)
    monitors = MonitorService(
        factory,
        EventBus(),
        _no_wake,
        Classifier(FakeProvider([]), ClassifierRegistry(Path("classifiers")), "m"),
    )
    store = MessageStore(factory)
    manager = SessionManager(
        provider=FakeProvider([]),
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="default-model",
        system_prompt="s",
        max_concurrent=4,
    )
    app = build_web_app(
        Auth("hunter2"), adapter, hub, handle, monitors, store, _palette, manager
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://web"
    )
    return client, adapter, monitors, store, manager, hub


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post("/login", data={"password": "hunter2"})
    assert response.status_code == 303
    client.cookies.update(response.cookies)


async def test_unauthed_root_shows_login(web: WebParts) -> None:
    client, *_ = web
    response = await client.get("/")
    assert "owner password" in response.text


async def test_wrong_password_is_rejected(web: WebParts) -> None:
    client, *_ = web
    response = await client.post("/login", data={"password": "nope"})
    assert response.status_code == 401
    assert "wrong password" in response.text


async def test_login_then_chat_page(web: WebParts) -> None:
    client, *_ = web
    await login(client)
    response = await client.get("/")
    assert "message chief" in response.text


async def test_send_requires_auth(web: WebParts) -> None:
    client, *_ = web
    response = await client.post("/send", json={"thread": "main", "text": "hi"})
    assert response.status_code == 401
    assert HANDLED == []


async def test_send_dispatches_an_owner_message(web: WebParts) -> None:
    client, *_ = web
    await login(client)
    response = await client.post("/send", json={"thread": "web:main", "text": "hi"})
    assert response.status_code == 202
    await asyncio.sleep(0)  # let the dispatched task run
    assert len(HANDLED) == 1
    message = HANDLED[0]
    assert message.channel == "web"
    assert message.sender == "owner"
    assert message.thread_key == "web:main"
    assert message.text == "hi"


async def test_send_omitted_thread_defaults_to_web_main(web: WebParts) -> None:
    client, *_ = web
    await login(client)
    response = await client.post("/send", json={"text": "hi"})
    assert response.status_code == 202
    await asyncio.sleep(0)
    assert HANDLED[0].channel == "web"
    assert HANDLED[0].thread_key == "web:main"


async def test_send_routes_to_the_threads_real_channel(web: WebParts) -> None:
    """A non-web thread keeps its real key and its origin channel (resolved from
    the store), so the turn's reply exits on that channel — not a web buffer."""
    client, _adapter, _monitors, store, _, _ = web
    await store.ensure_session("+15551234567", "imessage")
    await login(client)
    response = await client.post(
        "/send", json={"thread": "+15551234567", "text": "on my way"}
    )
    assert response.status_code == 202
    await asyncio.sleep(0)
    message = HANDLED[0]
    assert message.channel == "imessage"
    assert message.thread_key == "+15551234567"  # no web: prefix
    assert message.sender == "owner"


async def test_send_refuses_a_group_thread(web: WebParts) -> None:
    """Groups have no core send path (imsg only); the cockpit keeps them view-
    only, and the route refuses a send rather than run an undeliverable turn."""
    client, _adapter, _monitors, store, _, _ = web
    group = "3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c"
    await store.ensure_session(group, "imessage")
    await login(client)
    response = await client.post("/send", json={"thread": group, "text": "hey all"})
    assert response.status_code == 400
    await asyncio.sleep(0)
    assert HANDLED == []


async def test_adapter_broadcasts_frames_to_listeners() -> None:
    """A web-origin turn streams its delta/final to the browser via the hub."""
    hub = ObserverHub()
    adapter = WebAdapter(hub)
    queue = hub.listen()
    await adapter.send_delta("web:main", "he")
    await adapter.send("web:main", "hello")
    delta = queue.get_nowait()
    final = queue.get_nowait()
    assert delta == {"type": "delta", "thread": "web:main", "text": "he"}
    assert final == {"type": "final", "thread": "web:main", "text": "hello"}
    hub.drop(queue)
    await adapter.send("web:main", "gone")
    assert queue.empty()


async def test_hub_tick_broadcasts_to_listeners() -> None:
    """A coarse tick reaches every hub listener (the non-web observation path)."""
    hub = ObserverHub()
    queue = hub.listen()
    hub.tick("cli:t", "cli", "hey")
    assert queue.get_nowait() == {
        "type": "tick", "thread": "cli:t", "channel": "cli", "preview": "hey"
    }


async def test_events_requires_auth(web: WebParts) -> None:
    client, *_ = web
    response = await client.get("/events")
    assert response.status_code == 401


async def test_events_streams_a_tick_to_a_connected_client(web: WebParts) -> None:
    """The central mechanism end to end: an authed browser holding GET /events
    receives a hub tick as a real SSE `data:` frame — the leg the socket-side
    acceptance test can't exercise. (ASGITransport buffers the body, so the
    feeder closes the stream after the tick to let the response complete.)"""
    client, _, _, _, _, hub = web
    await login(client)

    async def feed() -> None:
        while not hub._queues:  # wait until the route registers its listener
            await asyncio.sleep(0)
        hub.tick("cli:home", "cli", "hi back")
        hub.close()

    feeder = asyncio.create_task(feed())
    try:
        async with client.stream("GET", "/events") as response:
            assert response.status_code == 200
            frames = [
                json.loads(line.removeprefix("data: "))
                async for line in response.aiter_lines()
                if line.startswith("data: ")
            ]
    finally:
        await feeder
    assert frames == [
        {"type": "tick", "thread": "cli:home",
         "channel": "cli", "preview": "hi back"},
    ]


async def test_monitor_list_route(web: WebParts) -> None:
    client, _, monitors, _store, _, _ = web
    await login(client)
    assert (await client.get("/monitors")).text == "none"
    await monitors.create(
        description="urgent watcher",
        watch_channel="cli",
        wake_channel="cli",
        wake_thread="cli:t",
        predicate={"kind": "code", "field": "text", "pattern": "x"},
    )
    assert "urgent watcher" in (await client.get("/monitors")).text


async def test_sessions_requires_auth(web: WebParts) -> None:
    client, *_ = web
    assert (await client.get("/sessions")).status_code == 401


async def test_sessions_lists_threads_with_metadata(web: WebParts) -> None:
    client, _, _, store, _, _ = web
    await login(client)
    await store.ensure_session("web:main", "web")
    await store.append("web:main", [{"role": "user", "content": "hi"}])
    await store.ensure_session("imessage:+1", "imessage")

    data = (await client.get("/sessions")).json()
    by_thread = {s["thread"]: s for s in data}
    assert by_thread["web:main"]["channel"] == "web"
    assert by_thread["web:main"]["count"] == 1
    assert by_thread["imessage:+1"]["channel"] == "imessage"


async def test_history_renders_owner_and_chief_rows(web: WebParts) -> None:
    client, _, _, store, _, _ = web
    await login(client)
    await store.ensure_session("web:main", "web")
    await store.append(
        "web:main",
        [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "tool_use", "id": "1", "name": "x", "input": {}},
                ],
            },
        ],
    )
    rows = (await client.get("/history?thread=web:main")).json()
    assert rows == [
        {"role": "owner", "text": "hi"},
        {"role": "chief", "text": "hello"},
    ]


async def test_history_without_thread_is_empty(web: WebParts) -> None:
    client, *_ = web
    await login(client)
    assert (await client.get("/history")).json() == []


async def test_history_includes_collapsed_tool_rows(web: WebParts) -> None:
    """A tool call shows as a name + call-id row, interleaved in order — no
    args or result in the list payload (#261)."""
    client, _, _, store, _ = web
    await login(client)
    await store.ensure_session("web:main", "web")
    await store.append(
        "web:main",
        [
            {"role": "user", "content": "read that file"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "x" * 5_000_000},
            {"role": "assistant", "content": "done"},
        ],
    )
    response = await client.get("/history?thread=web:main")
    assert len(response.content) < 10_000  # the huge tool result never leaks in
    assert response.json() == [
        {"role": "owner", "text": "read that file"},
        {"role": "tool", "call_id": "c1", "name": "read_file"},
        {"role": "chief", "text": "done"},
    ]


async def test_history_tool_requires_auth(web: WebParts) -> None:
    client, *_ = web
    response = await client.get("/history/tool?thread=web:main&call_id=c1")
    assert response.status_code == 401


async def test_history_tool_returns_args_and_result(web: WebParts) -> None:
    client, _, _, store, _ = web
    await login(client)
    await store.ensure_session("web:main", "web")
    await store.append(
        "web:main",
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "/tmp/x"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "file contents"},
        ],
    )
    response = await client.get("/history/tool?thread=web:main&call_id=c1")
    assert response.json() == {
        "status": "ok",
        "name": "read_file",
        "args": {"path": "/tmp/x"},
        "result": "file contents",
    }


async def test_history_tool_pending_when_result_not_yet_committed(
    web: WebParts,
) -> None:
    """The call landed but its result hasn't — a real mid-turn snapshot, not
    a fake status (#261's central mechanism: real stored wire messages)."""
    client, _, _, store, _ = web
    await login(client)
    await store.ensure_session("web:main", "web")
    await store.append(
        "web:main",
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "shell", "arguments": "{}"},
                    }
                ],
            }
        ],
    )
    response = await client.get("/history/tool?thread=web:main&call_id=c1")
    assert response.json() == {"status": "pending"}


async def test_history_tool_pending_while_call_id_unwritten_and_thread_busy(
    web: WebParts,
) -> None:
    """The call id isn't in the store at all yet — the turn hasn't produced
    it — but the thread is mid-turn, so this is pending, not an error."""
    client, _, _, store, manager = web
    await login(client)
    await store.ensure_session("web:main", "web")
    session = await manager.get_or_create("web:main", "web")
    async with session.lock:  # a turn holds this for its whole duration
        response = await client.get(
            "/history/tool?thread=web:main&call_id=never-committed"
        )
    assert response.json() == {"status": "pending"}


async def test_history_tool_compacted_when_call_id_is_gone(web: WebParts) -> None:
    client, _, _, store, _ = web
    await login(client)
    await store.ensure_session("web:main", "web")
    response = await client.get("/history/tool?thread=web:main&call_id=gone")
    assert response.json() == {"status": "compacted"}


async def test_commands_route_returns_palette(web: WebParts) -> None:
    client, *_ = web
    await login(client)
    assert (await client.get("/commands")).json() == ["/help", "/model", "/monitors"]


async def test_delete_requires_auth(web: WebParts) -> None:
    client, *_ = web
    response = await client.post("/delete", json={"thread": "web:scratch"})
    assert response.status_code == 401


async def test_delete_removes_a_scratch_buffer(web: WebParts) -> None:
    client, _, _, store, _, _ = web
    await login(client)
    await store.ensure_session("web:scratch", "web")
    await store.append("web:scratch", [{"role": "user", "content": "hi"}])

    response = await client.post("/delete", json={"thread": "web:scratch"})
    assert response.status_code == 200

    threads = {s["thread"] for s in await store.list_sessions()}
    assert "web:scratch" not in threads


async def test_delete_refuses_a_busy_buffer_with_409(web: WebParts) -> None:
    client, _, _, store, manager, _ = web
    await login(client)
    await store.ensure_session("web:scratch", "web")
    await store.append("web:scratch", [{"role": "user", "content": "hi"}])
    session = await manager.get_or_create("web:scratch", "web")
    async with session.lock:  # a turn holds this for its whole duration
        response = await client.post("/delete", json={"thread": "web:scratch"})
    assert response.status_code == 409
    threads = {s["thread"] for s in await store.list_sessions()}
    assert "web:scratch" in threads


async def test_delete_refuses_primary_and_non_web_threads(web: WebParts) -> None:
    client, _, _, store, _, _ = web
    await login(client)
    await store.ensure_session("web:main", "web")
    await store.ensure_session("imessage:+1", "imessage")

    primary = await client.post("/delete", json={"thread": "web:main"})
    assert primary.status_code == 400
    assert (
        await client.post("/delete", json={"thread": "imessage:+1"})
    ).status_code == 400

    threads = {s["thread"] for s in await store.list_sessions()}
    assert {"web:main", "imessage:+1"} <= threads


async def test_assets_are_served(web: WebParts) -> None:
    client, *_ = web
    css = await client.get("/app.css")
    assert css.headers["content-type"].startswith("text/css")
    assert "--orange" in css.text
    js = await client.get("/app.js")
    assert "javascript" in js.headers["content-type"]
    assert "EventSource" in js.text
