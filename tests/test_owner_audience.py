"""The non-owner output rule, end to end through build_app (PRD #308).

No turn in a thread whose surface reaches a non-owner auto-sends anything on
that surface — final text, notices, error replies, deltas, announcements or
approval cards — whoever triggered it. Cards go to the cockpit plus a pointer
on the owner's primary surface, and only the owner can answer them.
"""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from chief.adapters.base import Message
from chief.app import App, build_app

from .fakes import CapturingAdapter, FakeProvider, text_turn, tool_turn
from .test_app import boot, make_config, shutdown

STRANGER = "+15559990000"
OWNER_HANDLE = "+15550000001"
Frame = dict[str, Any]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@asynccontextmanager
async def running(
    tmp_path: Path, sock_path: Path, provider: FakeProvider, **overrides: Any
) -> AsyncIterator[tuple[App, CapturingAdapter, CapturingAdapter]]:
    """A booted daemon with a captured iMessage device and a captured web
    surface (the primary target when iMessage isn't enabled)."""
    config = make_config(
        tmp_path, sock_path, imessage_owner_handles=(OWNER_HANDLE,),
        web_password="hunter2", web_port=_free_port(), **overrides,
    )
    app, streams = await boot(config, provider)
    device = CapturingAdapter("imessage")
    web = CapturingAdapter("web")
    app.dispatcher.register(device)
    app.dispatcher.register(web)
    await app.store.ensure_session(STRANGER, "imessage")
    try:
        yield app, device, web
    finally:
        await shutdown(app, streams)


async def next_frame(queue: asyncio.Queue[Frame], kind: str, thread: str) -> Frame:
    while True:
        frame = await asyncio.wait_for(queue.get(), timeout=5)
        if frame["type"] == kind and frame.get("thread") == thread:
            return frame


def wake(thread: str, text: str = "wake up") -> Message:
    return Message(channel="imessage", sender="system", thread_key=thread, text=text)


async def assistant_texts(app: App, thread: str) -> list[str]:
    return [
        str(m["content"]) for m in await app.store.load(thread)
        if m["role"] == "assistant" and m.get("content")
    ]


async def test_system_wake_into_a_stranger_thread_sends_nothing(
    tmp_path: Path, sock_path: Path
) -> None:
    # A monitor/peer-shaped wake that runs an announced tool and replies: the
    # stranger's surface gets no announcement, no deltas, no final text.
    provider = FakeProvider([
        tool_turn("read_file", {"path": "packages/build-imessage/manifest.yaml"}),
        text_turn("noted, nothing to do"),
    ])
    async with running(tmp_path, sock_path, provider) as (app, device, _web):
        ticks = app.hub.listen()
        await app.dispatcher.handle(wake(STRANGER))
        tick = await next_frame(ticks, "tick", STRANGER)
        assert device.sent == []
        assert device.deltas == []
        assert tick["preview"] == "noted, nothing to do"
        assert await assistant_texts(app, STRANGER) == ["noted, nothing to do"]


async def test_failed_turn_in_a_stranger_thread_sends_no_error(
    tmp_path: Path, sock_path: Path
) -> None:
    async with running(tmp_path, sock_path, FakeProvider([])) as (app, device, _w):
        ticks = app.hub.listen()
        await app.dispatcher.handle(wake(STRANGER))
        tick = await next_frame(ticks, "tick", STRANGER)
        assert tick["preview"].startswith("error:")
        assert device.sent == []


async def test_cron_wake_into_a_stranger_thread_sends_nothing(
    tmp_path: Path, sock_path: Path
) -> None:
    config = make_config(
        tmp_path, sock_path, imessage_owner_handles=(OWNER_HANDLE,)
    )
    app = await build_app(config, provider=FakeProvider([text_turn("hi there")]))
    device = CapturingAdapter("imessage")
    app.dispatcher.register(device)
    await app.store.ensure_session(STRANGER, "imessage")
    schedule_id = await app.cron_service.create(
        description="nudge", spec="@every 0.05",
        wake_channel="imessage", wake_thread=STRANGER, prompt="say hi",
    )
    ticks = app.hub.listen()
    await app.start()
    streams = await asyncio.open_unix_connection(str(config.socket_path))
    try:
        await next_frame(ticks, "tick", STRANGER)
        await app.cron_service.delete(schedule_id)
        assert device.sent == []
        assert "hi there" in await assistant_texts(app, STRANGER)
    finally:
        await shutdown(app, streams)


async def test_owner_typing_into_a_stranger_thread_from_web_stays_quiet(
    tmp_path: Path, sock_path: Path
) -> None:
    provider = FakeProvider([text_turn("on my way")])
    async with running(tmp_path, sock_path, provider) as (app, device, _web):
        assert app.web_server is not None
        ticks = app.hub.listen()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.web_server.app),
            base_url="http://web",
        ) as client:
            resp = await client.post("/login", data={"password": "hunter2"})
            client.cookies.update(resp.cookies)
            resp = await client.post(
                "/send", json={"thread": STRANGER, "text": "you around?"}
            )
            assert resp.status_code == 202
            await next_frame(ticks, "tick", STRANGER)
        assert device.sent == []
        assert await assistant_texts(app, STRANGER) == ["on my way"]


async def test_stranger_thread_card_goes_to_the_cockpit_and_only_owner_answers(
    tmp_path: Path, sock_path: Path
) -> None:
    create = {
        "action": "create", "description": "check in",
        "spec": "0 9 * * *", "prompt": "say hi",
    }
    provider = FakeProvider([tool_turn("schedule", create), text_turn("scheduled")])
    async with running(tmp_path, sock_path, provider) as (app, device, web):
        assert app.web_server is not None
        watcher = app.hub.listen(STRANGER)
        ticks = app.hub.listen()
        turn = asyncio.create_task(app.dispatcher.handle(wake(STRANGER)))
        card = await next_frame(watcher, "approval", STRANGER)
        assert "approve tool call schedule" in card["question"]
        assert device.sent == []  # the stranger never sees the card
        assert len(web.sent) == 1
        pointer_thread, pointer = web.sent[0]
        assert pointer_thread == "web:main"
        assert STRANGER in pointer and "web cockpit" in pointer

        # The stranger's "yes" never answers the card.
        await app.dispatcher.handle(
            Message(channel="imessage", sender=STRANGER, thread_key=STRANGER,
                    text="yes")
        )
        assert app.dispatcher.resolve_approval(
            Message(channel="imessage", sender=STRANGER, thread_key=STRANGER,
                    text="yes")
        ) is False

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.web_server.app),
            base_url="http://web",
        ) as client:
            resp = await client.post("/login", data={"password": "hunter2"})
            client.cookies.update(resp.cookies)
            resp = await client.post(
                "/approve", json={"thread": STRANGER, "answer": "yes"}
            )
            assert resp.status_code == 200  # still pending: the "yes" didn't count
        await next_frame(ticks, "tick", STRANGER)
        await turn
        assert len(await app.cron_service.list_enabled()) == 1
        assert device.sent == []


async def test_owner_thread_output_is_unchanged(
    tmp_path: Path, sock_path: Path
) -> None:
    provider = FakeProvider([text_turn("morning")])
    async with running(tmp_path, sock_path, provider) as (app, device, _web):
        await app.dispatcher.handle(wake(OWNER_HANDLE))
        assert device.sent == [(OWNER_HANDLE, "morning")]
        assert device.deltas
