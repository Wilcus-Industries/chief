"""Web UI: auth, chat send, SSE frames, monitor list."""

import asyncio

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Message
from chief.bus import EventBus
from chief.monitors.service import ModelJudge, MonitorService
from chief.persistence.db import make_session_factory
from chief.web.adapter import WebAdapter
from chief.web.app import build_web_app
from chief.web.auth import Auth

from .fakes import FakeProvider

HANDLED: list[Message] = []


async def handle(message: Message) -> None:
    HANDLED.append(message)


async def _no_wake(message: Message) -> None:
    raise AssertionError("unexpected wake")


WebParts = tuple[httpx.AsyncClient, WebAdapter, MonitorService]


@pytest.fixture
def web(engine: AsyncEngine) -> WebParts:
    HANDLED.clear()
    adapter = WebAdapter()
    monitors = MonitorService(
        make_session_factory(engine),
        EventBus(),
        _no_wake,
        ModelJudge(FakeProvider([]), "m"),
    )
    app = build_web_app(Auth("hunter2"), adapter, handle, monitors)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://web"
    )
    return client, adapter, monitors


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
    client, _, monitors = web
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
