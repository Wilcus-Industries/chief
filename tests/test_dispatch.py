"""Dispatcher routing: approvals first, strangers dropped, bus publishing."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Adapter, Message
from chief.agent.manager import SessionManager
from chief.approvals import Approval, ApprovalBroker
from chief.bus import Event, EventBus
from chief.dispatch import Dispatcher
from chief.hub import ObserverHub
from chief.persistence.db import make_session_factory
from chief.persistence.store import MessageStore
from chief.policy import RICH, StreamPolicy
from chief.provider.base import ProviderError, ProviderEvent, ToolSpec
from chief.selfedit.restart import RestartController
from chief.strangers import StrangerLog
from chief.tools import Tool, ToolRegistry

from .fakes import FakeProvider, text_turn, tool_turn


class RecordingAdapter(Adapter):
    name = "cli"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, thread_key: str, text: str) -> None:
        self.sent.append((thread_key, text))


def make_manager(provider: FakeProvider, store: MessageStore) -> SessionManager:
    return SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="m",
        system_prompt="s",
        max_concurrent=4,
    )


def owner_message(text: str, sender: str = "owner") -> Message:
    return Message(channel="cli", sender=sender, thread_key="cli:t", text=text)


async def test_approval_answer_is_consumed_not_dispatched(
    store: MessageStore,
) -> None:
    provider = FakeProvider([])
    approvals = ApprovalBroker()
    dispatcher = Dispatcher(make_manager(provider, store), approvals=approvals)
    adapter = RecordingAdapter()
    dispatcher.register(adapter)
    card = asyncio.create_task(
        approvals.ask("cli:t", "ok?", lambda q: adapter.send("cli:t", q))
    )
    await asyncio.sleep(0.01)
    await dispatcher.handle(owner_message("yes"))
    assert await card is Approval.ONCE
    assert provider.calls == []  # the yes never became a turn


async def test_resolve_approval_answers_pending_card_without_a_turn(
    store: MessageStore,
) -> None:
    """The public resolver (used by iMessage's poll stage to bypass its FIFO
    worker) consumes an answer to a pending card and reports True; with no
    card pending it reports False so the message would run a turn."""
    provider = FakeProvider([])
    approvals = ApprovalBroker()
    dispatcher = Dispatcher(make_manager(provider, store), approvals=approvals)
    adapter = RecordingAdapter()
    dispatcher.register(adapter)
    assert dispatcher.resolve_approval(owner_message("yes")) is False  # no card
    card = asyncio.create_task(
        approvals.ask("cli:t", "ok?", lambda q: adapter.send("cli:t", q))
    )
    await asyncio.sleep(0.01)
    assert dispatcher.resolve_approval(owner_message("yes")) is True
    assert await card is Approval.ONCE
    assert provider.calls == []


async def test_stranger_is_logged_and_never_answered(
    engine: AsyncEngine, store: MessageStore
) -> None:
    factory = make_session_factory(engine)
    strangers = StrangerLog(factory)
    provider = FakeProvider([])
    dispatcher = Dispatcher(make_manager(provider, store), strangers=strangers)
    adapter = RecordingAdapter()
    dispatcher.register(adapter)
    await dispatcher.handle(owner_message("hello?", sender="unknown-5551234"))
    assert provider.calls == []
    assert adapter.sent == []
    rows = await strangers.list_recent()
    assert len(rows) == 1
    assert rows[0].sender == "unknown-5551234"


async def test_stranger_is_published_for_monitors_but_runs_no_turn(
    engine: AsyncEngine, store: MessageStore
) -> None:
    bus = EventBus()
    seen: list[Event] = []

    async def collector(event: Event) -> None:
        seen.append(event)

    bus.subscribe(collector)
    strangers = StrangerLog(make_session_factory(engine))
    provider = FakeProvider([])
    dispatcher = Dispatcher(
        make_manager(provider, store), bus=bus, strangers=strangers
    )
    dispatcher.register(RecordingAdapter())
    await dispatcher.handle(owner_message("watch me", sender="+15559998888"))
    assert provider.calls == []
    assert [e.payload["sender"] for e in seen] == ["+15559998888"]
    assert seen[0].payload["text"] == "watch me"


async def test_selfedit_turn_fires_restart_after_reply_sent(
    store: MessageStore,
) -> None:
    """A self-edit turn requests a restart mid-turn; the dispatcher fires the
    execv only after the reply has been sent, so the reply is never lost."""
    adapter = RecordingAdapter()
    fired: list[list[tuple[str, str]]] = []
    controller = RestartController(lambda: fired.append(list(adapter.sent)))

    registry = ToolRegistry()

    async def fake_restart() -> str:
        controller.request()
        return "restarting"

    registry.register(
        Tool(ToolSpec(name="restart", description="", parameters={}), fake_restart)
    )
    provider = FakeProvider([tool_turn("restart", {}), text_turn("done")])
    manager = SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: registry,
        store=store,
        default_model="m",
        system_prompt="s",
        max_concurrent=4,
        restart_gate=controller,
    )
    dispatcher = Dispatcher(manager, restart=controller)
    dispatcher.register(adapter)

    await dispatcher.handle(owner_message("self-edit please"))

    # Restart fired exactly once, and the reply was already sent when it did.
    assert fired == [[("cli:t", "done")]]


class _DownProvider:
    """A backend whose every turn fails with a loud ProviderError."""

    def __init__(self, message: str) -> None:
        self._message = message

    async def stream(self, **_: object) -> AsyncIterator[ProviderEvent]:
        raise ProviderError(self._message)
        yield  # pragma: no cover - marks this coroutine an async generator


class _BoomProvider:
    """A backend that fails with a non-provider exception (a real bug)."""

    async def stream(self, **_: object) -> AsyncIterator[ProviderEvent]:
        raise RuntimeError("kaboom: a secret traceback detail")
        yield  # pragma: no cover - marks this coroutine an async generator


async def test_provider_error_message_is_surfaced_to_owner(
    store: MessageStore,
) -> None:
    provider = _DownProvider("backend unreachable (http://proxy/v1, model m)")
    dispatcher = Dispatcher(make_manager(provider, store))  # type: ignore[arg-type]
    adapter = RecordingAdapter()
    dispatcher.register(adapter)
    await dispatcher.handle(owner_message("hi"))
    assert adapter.sent == [("cli:t", "error: backend unreachable "
                             "(http://proxy/v1, model m)")]


async def test_other_exceptions_keep_the_generic_text(store: MessageStore) -> None:
    dispatcher = Dispatcher(make_manager(_BoomProvider(), store))  # type: ignore[arg-type]
    adapter = RecordingAdapter()
    dispatcher.register(adapter)
    await dispatcher.handle(owner_message("hi"))
    # The traceback detail never leaks; the owner gets the generic string.
    assert adapter.sent == [
        ("cli:t", "error: something went wrong running that turn")
    ]


async def test_owner_message_is_published_system_wake_is_not(
    store: MessageStore,
) -> None:
    bus = EventBus()
    seen: list[Event] = []

    async def collector(event: Event) -> None:
        seen.append(event)

    bus.subscribe(collector)
    provider = FakeProvider([text_turn("a"), text_turn("b")])
    dispatcher = Dispatcher(make_manager(provider, store), bus=bus)
    dispatcher.register(RecordingAdapter())
    await dispatcher.handle(owner_message("from owner"))
    await dispatcher.handle(owner_message("wake!", sender="system"))
    inbound = [e.payload["text"] for e in seen if e.type == "message.inbound"]
    assert inbound == ["from owner"]
    assert len(provider.calls) == 2  # both still ran turns


async def test_completed_turn_emits_one_tick(store: MessageStore) -> None:
    """A completed non-web turn puts exactly one coarse ``tick`` on every hub
    listener — and nothing else — while the reply still exits its own channel."""
    hub = ObserverHub()
    provider = FakeProvider([text_turn("hello back")])
    dispatcher = Dispatcher(make_manager(provider, store), hub=hub)
    adapter = RecordingAdapter()
    dispatcher.register(adapter)
    queue = hub.listen()
    await dispatcher.handle(owner_message("hi"))
    assert queue.get_nowait() == {
        "type": "tick", "thread": "cli:t", "channel": "cli", "preview": "hello back"
    }
    assert queue.empty()  # no delta/final/peer frames for an un-tapped thread
    assert adapter.sent == [("cli:t", "hello back")]  # origin channel unaffected


def _read_file_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def read_file(**_: object) -> str:
        return "file contents"

    registry.register(
        Tool(ToolSpec(name="read_file", description="", parameters={}), read_file)
    )
    return registry


def _drain(queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    return frames


async def test_watched_thread_streams_rich_frames(store: MessageStore) -> None:
    """A client tapped into a non-web thread receives that turn's inbound text,
    the name-only tool tick as the call fires, the reply deltas, and the final
    — while the reply still exits on its own channel."""
    hub = ObserverHub()
    registry = _read_file_registry()
    manager = SessionManager(
        provider=FakeProvider(
            [tool_turn("read_file", {}, call_id="c1"), text_turn("done")]
        ),
        tools_factory=lambda thread, channel: registry,
        store=store,
        default_model="m",
        system_prompt="s",
        max_concurrent=4,
    )
    # cli defaults coarse now; opt this thread's channel into RICH so the rich
    # frames still flow (the #262/#263 behavior, under the new policy gate).
    dispatcher = Dispatcher(manager, hub=hub, channel_defaults={"cli": RICH})
    adapter = RecordingAdapter()
    dispatcher.register(adapter)
    queue = hub.listen("cli:t")

    await dispatcher.handle(owner_message("hi"))

    frames = _drain(queue)
    assert frames[0] == {"type": "inbound", "thread": "cli:t", "text": "hi"}
    # Name only — no args/result (they load via the lazy /history/tool fetch).
    assert frames[1] == {
        "type": "tool", "thread": "cli:t", "call_id": "c1", "name": "read_file"
    }
    deltas = [f for f in frames if f["type"] == "delta"]
    assert "".join(f["text"] for f in deltas) == "done"
    assert frames[-2] == {"type": "final", "thread": "cli:t", "text": "done"}
    assert frames[-1] == {
        "type": "tick", "thread": "cli:t", "channel": "cli", "preview": "done"
    }
    assert adapter.sent == [("cli:t", "done")]  # origin channel still delivered


def _tool_manager(store: MessageStore) -> SessionManager:
    registry = _read_file_registry()
    return SessionManager(
        provider=FakeProvider(
            [tool_turn("read_file", {}, call_id="c1"), text_turn("done")]
        ),
        tools_factory=lambda thread, channel: registry,
        store=store,
        default_model="m",
        system_prompt="s",
        max_concurrent=4,
    )


async def _run_tapped(
    store: MessageStore, policy: StreamPolicy
) -> list[dict[str, Any]]:
    """Run one tool+text turn on a tapped ``cli:t`` under ``policy`` for cli."""
    hub = ObserverHub()
    dispatcher = Dispatcher(
        _tool_manager(store), hub=hub, channel_defaults={"cli": policy}
    )
    dispatcher.register(RecordingAdapter())
    queue = hub.listen("cli:t")
    await dispatcher.handle(owner_message("hi"))
    return _drain(queue)


async def test_deltas_off_keeps_tool_tick_and_final_but_no_delta(
    store: MessageStore,
) -> None:
    frames = await _run_tapped(store, StreamPolicy(tools=True, results="lazy"))
    types = [f["type"] for f in frames]
    assert "delta" not in types
    assert {"type": "tool", "thread": "cli:t", "call_id": "c1",
            "name": "read_file"} in frames
    assert any(f["type"] == "final" for f in frames)


async def test_tools_off_drops_the_tool_frame(store: MessageStore) -> None:
    frames = await _run_tapped(store, StreamPolicy(deltas=True))
    types = [f["type"] for f in frames]
    assert "tool" not in types
    assert "delta" in types and "final" in types


async def test_results_inline_emits_a_result_frame_with_the_body(
    store: MessageStore,
) -> None:
    frames = await _run_tapped(store, StreamPolicy(tools=True, results="inline"))
    tool_idx = next(i for i, f in enumerate(frames) if f["type"] == "tool")
    result = next(f for f in frames if f["type"] == "result")
    assert result == {"type": "result", "thread": "cli:t",
                      "call_id": "c1", "result": "file contents"}
    # The result lands after its tool tick, as the call fires.
    assert frames.index(result) > tool_idx


async def test_results_off_drops_the_call_id_from_the_tool_frame(
    store: MessageStore,
) -> None:
    frames = await _run_tapped(store, StreamPolicy(tools=True, results="off"))
    tool = next(f for f in frames if f["type"] == "tool")
    assert tool == {"type": "tool", "thread": "cli:t", "name": "read_file"}
    assert "call_id" not in tool


async def test_coarse_default_taps_only_inbound_final_tick(
    store: MessageStore,
) -> None:
    frames = await _run_tapped(store, StreamPolicy())  # all off
    assert [f["type"] for f in frames] == ["inbound", "final", "tick"]


async def test_unwatched_thread_emits_only_the_coarse_tick(
    store: MessageStore,
) -> None:
    """No subscriber tapped into the thread: the turn puts only the single
    coarse tick on the wire — no inbound/tool/delta/final frames (AC2)."""
    hub = ObserverHub()
    registry = _read_file_registry()
    manager = SessionManager(
        provider=FakeProvider(
            [tool_turn("read_file", {}, call_id="c1"), text_turn("done")]
        ),
        tools_factory=lambda thread, channel: registry,
        store=store,
        default_model="m",
        system_prompt="s",
        max_concurrent=4,
    )
    dispatcher = Dispatcher(manager, hub=hub)
    dispatcher.register(RecordingAdapter())
    queue = hub.listen()  # watches no thread

    await dispatcher.handle(owner_message("hi"))

    assert queue.get_nowait() == {
        "type": "tick", "thread": "cli:t", "channel": "cli", "preview": "done"
    }
    assert queue.empty()


async def test_system_wake_ticks_like_owner(store: MessageStore) -> None:
    """A system-sender (monitor/cron) turn ticks identically to an owner turn."""
    hub = ObserverHub()
    provider = FakeProvider([text_turn("hello back")])
    dispatcher = Dispatcher(make_manager(provider, store), hub=hub)
    dispatcher.register(RecordingAdapter())
    queue = hub.listen()
    await dispatcher.handle(owner_message("hi", sender="system"))
    assert queue.get_nowait() == {
        "type": "tick", "thread": "cli:t", "channel": "cli", "preview": "hello back"
    }
    assert queue.empty()


class _WebAdapter(RecordingAdapter):
    name = "web"


async def test_web_origin_turn_still_emits_the_coarse_tick(
    store: MessageStore,
) -> None:
    """A web-origin turn streams its own rich final through the adapter, but
    still emits the one coarse tick so other tabs get the unread/reorder/
    snippet (an un-focused or abandoned web buffer would go dark otherwise)."""
    hub = ObserverHub()
    provider = FakeProvider([text_turn("hello back")])
    dispatcher = Dispatcher(make_manager(provider, store), hub=hub)
    dispatcher.register(_WebAdapter())
    queue = hub.listen()
    await dispatcher.handle(
        Message(channel="web", sender="owner", thread_key="web:main", text="hi")
    )
    assert queue.get_nowait() == {
        "type": "tick", "thread": "web:main",
        "channel": "web", "preview": "hello back",
    }
    assert queue.empty()


async def test_web_origin_turn_omits_the_rich_final(store: MessageStore) -> None:
    """The client tapped into a web thread gets the coarse tick but not a
    dispatcher `final` — the WebAdapter already streamed the rich final, so a
    second one would double-count the focused thread."""
    hub = ObserverHub()
    provider = FakeProvider([text_turn("hello back")])
    dispatcher = Dispatcher(make_manager(provider, store), hub=hub)
    dispatcher.register(_WebAdapter())
    queue = hub.listen("web:main")
    await dispatcher.handle(
        Message(channel="web", sender="owner", thread_key="web:main", text="hi")
    )
    frames = _drain(queue)
    assert not any(f["type"] == "final" for f in frames)
    assert frames == [{
        "type": "tick", "thread": "web:main",
        "channel": "web", "preview": "hello back",
    }]
