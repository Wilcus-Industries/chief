"""Mid-turn injection: an owner message offered to a running turn lands in its
transcript at the next tool boundary instead of queuing behind the turn."""

import asyncio
from typing import Any

from chief.adapters.base import Message
from chief.agent.inbox import Inbox, Injected
from chief.agent.loop import run_turn
from chief.agent.manager import SessionManager
from chief.bus import Event, EventBus
from chief.dispatch import Dispatcher
from chief.hub import ObserverHub
from chief.persistence.store import MessageStore
from chief.provider.base import ProviderEvent, ToolSpec
from chief.tools import Tool, ToolRegistry

from .fakes import FakeProvider, text_turn, tool_turn

WAIT_SPEC = ToolSpec(
    name="wait", description="Block until released.",
    parameters={"type": "object", "properties": {}},
)


class Gate:
    """A tool that parks the turn mid-dispatch, so a test can inject while
    the turn is provably in flight."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def registry(self) -> ToolRegistry:
        async def wait() -> str:
            self.entered.set()
            await self.release.wait()
            return "waited"

        registry = ToolRegistry()
        registry.register(Tool(spec=WAIT_SPEC, handler=wait))
        return registry


def manager_for(
    script: list[list[ProviderEvent]], store: MessageStore, registry: ToolRegistry
) -> tuple[FakeProvider, SessionManager]:
    provider = FakeProvider(script)
    manager = SessionManager(
        provider=provider, tools_factory=lambda thread, channel: registry,
        store=store, default_model="m", system_prompt="s", max_concurrent=4,
        soul_reader=lambda: "",
    )
    return provider, manager


async def noop_delta(text: str) -> None:
    pass


def item(text: str, log: list[str]) -> Injected:
    async def consumed() -> None:
        log.append(f"consumed:{text}")

    return Injected(text, consumed, lambda: log.append(f"leftover:{text}"))


# ---- inbox ----


async def test_closed_inbox_refuses_offers() -> None:
    log: list[str] = []
    assert Inbox().offer(item("hi", log)) is False
    assert log == []


async def test_inbox_take_drains_in_order_and_close_requeues_the_rest() -> None:
    inbox, log = Inbox(), list[str]()
    inbox.open()
    assert inbox.offer(item("a", log)) and inbox.offer(item("b", log))
    assert await inbox.take() == ["a", "b"]
    inbox.offer(item("c", log))
    inbox.close()
    assert log == ["consumed:a", "consumed:b", "leftover:c"]
    assert inbox.offer(item("d", log)) is False


# ---- loop ----


async def test_loop_appends_injected_text_before_the_next_model_call() -> None:
    gate = Gate()
    gate.release.set()
    provider = FakeProvider([tool_turn("wait", {}), text_turn("saw it")])
    pending = ["stop, do X instead"]

    async def take() -> list[str]:
        out = list(pending)
        pending.clear()
        return out

    committed: list[dict[str, Any]] = []

    async def commit(message: dict[str, Any]) -> None:
        committed.append(message)

    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    result = await run_turn(
        provider=provider, model="m", messages=messages, tools=gate.registry(),
        on_delta=noop_delta, on_commit=commit, take_injected=take,
    )
    assert result.text == "saw it"
    injected = {"role": "user", "content": "stop, do X instead"}
    # After the tool result, before the second model call — and persisted.
    assert [m["role"] for m in messages] == [
        "user", "assistant", "tool", "user", "assistant"
    ]
    assert provider.calls[1][-1] == injected
    assert injected in committed


# ---- session ----


async def test_session_injects_into_a_running_turn(store: MessageStore) -> None:
    gate = Gate()
    provider, manager = manager_for(
        [tool_turn("wait", {}), text_turn("ok, switching")], store, gate.registry()
    )
    session = await manager.get_or_create("cli:t", "cli")
    log: list[str] = []
    assert session.inject(item("early", log)) is False  # idle: caller queues
    turn = asyncio.create_task(session.run_turn("start", noop_delta))
    await gate.entered.wait()
    assert session.inject(item("change course", log)) is True
    gate.release.set()
    result = await turn
    assert result.text == "ok, switching"
    assert {"role": "user", "content": "change course"} in provider.calls[1]
    assert log == ["consumed:change course"]
    stored = await store.load("cli:t")
    assert {"role": "user", "content": "change course"} in stored


async def test_session_requeues_an_injection_the_turn_never_reached(
    store: MessageStore,
) -> None:
    """A text-only turn has no tool boundary: the offer is handed back, not lost."""
    provider, manager = manager_for([text_turn("done")], store, ToolRegistry())
    provider.gate = asyncio.Event()
    session = await manager.get_or_create("cli:t", "cli")
    log: list[str] = []
    turn = asyncio.create_task(session.run_turn("start", noop_delta))
    await asyncio.sleep(0.01)
    assert session.inject(item("late", log)) is True
    provider.gate.set()
    await turn
    assert log == ["leftover:late"]


async def test_on_begin_fires_once_the_turn_holds_the_lock(
    store: MessageStore,
) -> None:
    provider, manager = manager_for(
        [text_turn("one"), text_turn("two")], store, ToolRegistry()
    )
    session = await manager.get_or_create("cli:t", "cli")
    began: list[str] = []

    def begin(label: str) -> Any:
        async def on_begin() -> None:
            began.append(label)
        return on_begin

    await asyncio.gather(
        session.run_turn("a", noop_delta, on_begin=begin("a")),
        session.run_turn("b", noop_delta, on_begin=begin("b")),
    )
    assert began == ["a", "b"]


# ---- dispatcher ----


def owner(text: str, sender: str = "owner", channel: str = "cli") -> Message:
    return Message(channel=channel, sender=sender, thread_key="cli:t", text=text)


async def test_try_inject_refuses_strangers_commands_and_unknown_threads(
    store: MessageStore,
) -> None:
    _, manager = manager_for([], store, ToolRegistry())
    dispatcher = Dispatcher(manager)
    requeued: list[Message] = []
    assert dispatcher.try_inject(owner("hi"), requeued.append) is False  # no session
    await manager.get_or_create("cli:t", "cli")
    assert dispatcher.try_inject(owner("hi"), requeued.append) is False  # idle
    assert dispatcher.try_inject(owner("hi", sender="+1555"), requeued.append) is False
    assert dispatcher.try_inject(owner("/stop"), requeued.append) is False
    assert requeued == []


async def test_injected_message_streams_inbound_and_publishes_on_consume(
    store: MessageStore,
) -> None:
    gate = Gate()
    _, manager = manager_for(
        [tool_turn("wait", {}), text_turn("noted")], store, gate.registry()
    )
    hub, bus = ObserverHub(), EventBus()
    seen: list[Event] = []

    async def record(event: Event) -> None:
        seen.append(event)

    bus.subscribe(record)
    dispatcher = Dispatcher(manager, hub=hub, bus=bus)
    from .test_dispatch import RecordingAdapter

    dispatcher.register(RecordingAdapter())
    queue = hub.listen("cli:t")
    turn = asyncio.create_task(dispatcher.handle(owner("start")))
    await gate.entered.wait()
    requeued: list[Message] = []
    assert dispatcher.try_inject(owner("also this"), requeued.append) is True
    gate.release.set()
    await turn
    inbound = []
    while not queue.empty():
        frame = queue.get_nowait()
        if frame["type"] == "inbound":
            inbound.append(frame["text"])
    assert inbound == ["start", "also this"]
    texts = [e.payload["text"] for e in seen if e.type == "message.inbound"]
    assert texts == ["start", "also this"]
    assert requeued == []


async def test_leftover_injection_is_requeued_through_the_callback(
    store: MessageStore,
) -> None:
    provider, manager = manager_for([text_turn("done")], store, ToolRegistry())
    provider.gate = asyncio.Event()
    dispatcher = Dispatcher(manager)
    from .test_dispatch import RecordingAdapter

    dispatcher.register(RecordingAdapter())
    turn = asyncio.create_task(dispatcher.handle(owner("start")))
    await asyncio.sleep(0.01)
    requeued: list[Message] = []
    late = owner("late")
    assert dispatcher.try_inject(late, requeued.append) is True
    provider.gate.set()
    await turn
    assert requeued == [late]


async def test_web_turn_streams_its_inbound_when_the_turn_begins(
    store: MessageStore,
) -> None:
    """The web client renders an owner message from this frame, so a message
    queued behind a running turn appears when its turn starts — not wiped by
    a re-render before it was ever persisted."""
    _, manager = manager_for([text_turn("hey")], store, ToolRegistry())
    hub = ObserverHub()
    dispatcher = Dispatcher(manager, hub=hub)
    from .test_dispatch import _WebAdapter

    dispatcher.register(_WebAdapter())
    queue = hub.listen("web:main")
    await dispatcher.handle(
        Message(channel="web", sender="owner", thread_key="web:main", text="hi")
    )
    assert queue.get_nowait() == {"type": "inbound", "thread": "web:main", "text": "hi"}
