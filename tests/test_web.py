"""Web UI: auth, chat send, SSE frames, monitor list."""

import asyncio

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Message
from chief.agent.manager import SessionManager
from chief.agent.tools import ToolRegistry
from chief.bus import EventBus
from chief.monitors.service import ModelJudge, MonitorService
from chief.persistence.db import make_session_factory
from chief.persistence.store import MessageStore
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


WebParts = tuple[httpx.AsyncClient, WebAdapter, MonitorService, MessageStore]


@pytest.fixture
def web(engine: AsyncEngine) -> WebParts:
    HANDLED.clear()
    adapter = WebAdapter()
    factory = make_session_factory(engine)
    monitors = MonitorService(
        factory,
        EventBus(),
        _no_wake,
        ModelJudge(FakeProvider([]), "m"),
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
        Auth("hunter2"), adapter, handle, monitors, store, _palette, manager
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://web"
    )
    return client, adapter, monitors, store


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
    response = await client.post("/send", json={"thread": "main", "text": "hi"})
    assert response.status_code == 202
    await asyncio.sleep(0)  # let the dispatched task run
    assert len(HANDLED) == 1
    message = HANDLED[0]
    assert message.channel == "web"
    assert message.sender == "owner"
    assert message.thread_key == "web:main"
    assert message.text == "hi"


async def test_adapter_broadcasts_frames_to_listeners() -> None:
    adapter = WebAdapter()
    queue = adapter.listen()
    await adapter.send_delta("web:main", "he")
    await adapter.send("web:main", "hello")
    delta = queue.get_nowait()
    final = queue.get_nowait()
    assert delta == {"type": "delta", "thread": "web:main", "text": "he"}
    assert final == {"type": "final", "thread": "web:main", "text": "hello"}
    adapter.drop(queue)
    await adapter.send("web:main", "gone")
    assert queue.empty()


async def test_events_requires_auth(web: WebParts) -> None:
    client, *_ = web
    response = await client.get("/events")
    assert response.status_code == 401


async def test_monitor_list_route(web: WebParts) -> None:
    client, _, monitors, _store = web
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
    client, _, _, store = web
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
    client, _, _, store = web
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


async def test_commands_route_returns_palette(web: WebParts) -> None:
    client, *_ = web
    await login(client)
    assert (await client.get("/commands")).json() == ["/help", "/model", "/monitors"]


async def test_delete_requires_auth(web: WebParts) -> None:
    client, *_ = web
    response = await client.post("/delete", json={"thread": "web:scratch"})
    assert response.status_code == 401


async def test_delete_removes_a_scratch_buffer(web: WebParts) -> None:
    client, _, _, store = web
    await login(client)
    await store.ensure_session("web:scratch", "web")
    await store.append("web:scratch", [{"role": "user", "content": "hi"}])

    response = await client.post("/delete", json={"thread": "web:scratch"})
    assert response.status_code == 200

    threads = {s["thread"] for s in await store.list_sessions()}
    assert "web:scratch" not in threads


async def test_delete_refuses_primary_and_non_web_threads(web: WebParts) -> None:
    client, _, _, store = web
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
