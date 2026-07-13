"""Web chat + SSE integration tests (#153).

Real HTTP against the real ASGI app, which fronts a real SocketServer + CliAdapter +
TaskManager; the ONLY fake is the LLM (``FakeSession``, the established seam). The
SSE stream is read for real — the central mechanism (HTTP + SSE over the client-plane
frame vocabulary) is never mocked.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.cli import ForeignPlatform
from chief.core.session import Milestone
from chief.persistence.tasks import get_or_create_task
from test_broadcast_bus import _RecordingInner
from test_cli_platform import _mirror_manager, _seq_factory
from test_tasks import FakeSession, wait_for_task_open
from web_helpers import SseReader, WebStack, start_web_stack


@pytest.fixture
async def chat_stack(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[WebStack]:
    session = FakeSession(model="m", milestones=[Milestone(text="using Bash")])
    stack = await start_web_stack(
        tmp_path, session_factory, sdk_factory=_seq_factory([session])
    )
    try:
        yield stack
    finally:
        await stack.aclose()


async def test_send_echoes_owner_line_then_streams_milestone_and_reply(
    chat_stack: WebStack,
) -> None:
    async with SseReader(
        chat_stack.client, "/events?platform=cli&thread_key=cli:main"
    ) as sse:
        resp = await chat_stack.client.post(
            "/chat/send",
            data={"platform": "cli", "thread_key": "cli:main", "text": "go"},
        )
        assert resp.status_code == 200
        assert "msg owner" in resp.text and "go" in resp.text

        event, data = await sse.next_event()
        assert event == "message"
        assert "using Bash" in data and "milestone" in data

        event, data = await sse.next_event()
        assert event == "message"
        assert "reply:go" in data and "msg chief" in data


async def test_chat_page_renders_backfill_and_thread_list(
    chat_stack: WebStack, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    resp = await chat_stack.client.post(
        "/chat/send",
        data={"platform": "cli", "thread_key": "cli:main", "text": "hello"},
    )
    assert resp.status_code == 200
    await wait_for_task_open(session_factory, platform="cli", thread_key="cli:main")

    resp = await chat_stack.client.get("/chat?platform=cli&thread_key=cli:main")
    assert resp.status_code == 200
    assert "hello" in resp.text  # the owner line, from the message log backfill
    assert "reply:hello" in resp.text  # the chief reply
    assert "/events?platform=cli&amp;thread_key=cli%3Amain" in resp.text


async def test_empty_thread_renders_without_history(chat_stack: WebStack) -> None:
    # cli:main has no task row yet — switch answers unknown_thread; the page still
    # renders (an empty transcript), because day one starts with no history at all.
    resp = await chat_stack.client.get("/chat")
    assert resp.status_code == 200
    assert "transcript" in resp.text


async def test_send_rejects_missing_text(chat_stack: WebStack) -> None:
    resp = await chat_stack.client.post(
        "/chat/send", data={"platform": "cli", "thread_key": "cli:main", "text": ""}
    )
    assert resp.status_code == 400


async def test_slash_command_routes_through_the_owner_registry(
    chat_stack: WebStack,
) -> None:
    async with SseReader(
        chat_stack.client, "/events?platform=cli&thread_key=cli:main"
    ) as sse:
        resp = await chat_stack.client.post(
            "/chat/send",
            data={"platform": "cli", "thread_key": "cli:main", "text": "/tasks"},
        )
        assert resp.status_code == 200
        event, data = await sse.next_event()
        assert event == "message"
        assert "No active tasks." in data


async def test_threads_partial_lists_every_platforms_threads(
    chat_stack: WebStack, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        await get_or_create_task(
            session,
            platform="telegram",
            thread_key="tg:9",
            tier="owner",
            title="phone thread",
        )
        await session.commit()
    resp = await chat_stack.client.get(
        "/chat/threads?platform=cli&thread_key=cli:main"
    )
    assert resp.status_code == 200
    assert "phone thread" in resp.text
    assert "thread_key=tg%3A9" in resp.text


async def test_new_thread_mints_distinct_keys(chat_stack: WebStack) -> None:
    first = await chat_stack.client.get("/chat/new")
    second = await chat_stack.client.get("/chat/new")
    assert first.status_code == 303 and second.status_code == 303
    assert first.headers["location"] != second.headers["location"]
    assert "platform=cli" in first.headers["location"]


async def test_cancel_control_stops_the_running_turn(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    import asyncio

    started = asyncio.Event()
    gate = asyncio.Event()  # never set — the turn parks until cancelled
    session = FakeSession(model="m", gate=gate, on_start=started.set)
    stack = await start_web_stack(
        tmp_path, session_factory, sdk_factory=_seq_factory([session])
    )
    try:
        async with SseReader(
            stack.client, "/events?platform=cli&thread_key=cli:main"
        ) as sse:
            resp = await stack.client.post(
                "/chat/send",
                data={"platform": "cli", "thread_key": "cli:main", "text": "long"},
            )
            assert resp.status_code == 200
            await asyncio.wait_for(started.wait(), 5)

            resp = await stack.client.post(
                "/chat/cancel",
                data={"platform": "cli", "thread_key": "cli:main"},
            )
            assert resp.status_code == 200
            event, data = await sse.next_event()
            assert "Cancelled." in data
    finally:
        await stack.aclose()


async def test_foreign_platform_send_injects_and_streams_the_reply(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Parity across surfaces: a telegram thread driven from the browser echoes onto
    # the (fake) phone chat and its reply mirrors back to the web via the broadcast.
    foreign: dict[str, ForeignPlatform] = {}
    stack = await start_web_stack(
        tmp_path,
        session_factory,
        sdk_factory=_seq_factory([]),
        foreign=foreign,
    )
    inner = _RecordingInner()
    telegram_manager, _ = _mirror_manager(
        session_factory,
        stack.server,
        inner,
        factory=_seq_factory([FakeSession(model="m")]),
    )
    stack.extra_managers.append(telegram_manager)
    # The RAW platform io, exactly as build_stacks populates foreign_out (#135).
    foreign["telegram"] = ForeignPlatform(engine=telegram_manager, io=inner)
    async with session_factory() as session:
        await get_or_create_task(
            session,
            platform="telegram",
            thread_key="tg:1",
            tier="owner",
            title="phone",
            surface="dm",
        )
        await session.commit()

    try:
        async with SseReader(
            stack.client, "/events?platform=telegram&thread_key=tg:1"
        ) as sse:
            resp = await stack.client.post(
                "/chat/send",
                data={
                    "platform": "telegram",
                    "thread_key": "tg:1",
                    "text": "from the browser",
                },
            )
            assert resp.status_code == 200
            event, data = await sse.next_event()
            assert event == "message"
            assert "reply:from the browser" in data
        # The phone-side history got the via-CLI echo before the dispatch (#135).
        assert any("from the browser" in text for _, text in inner.sends)
    finally:
        await stack.aclose()
