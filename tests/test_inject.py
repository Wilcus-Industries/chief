"""Mid-turn injection: an owner message offered to a running turn lands in its
transcript at the next tool boundary instead of queuing behind the turn."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from chief.adapters.base import Message
from chief.agent.inbox import Inbox, Injected
from chief.agent.loop import run_turn
from chief.agent.manager import SessionManager
from chief.bus import Event, EventBus
from chief.dispatch import Dispatcher
from chief.hub import ObserverHub
from chief.persistence.store import MessageStore
from chief.provider.base import ProviderError, ProviderEvent, ToolSpec
from chief.tools import Tool, ToolRegistry

from .fakes import FakeProvider, text_turn, tool_turn
from .test_dispatch import RecordingAdapter, _WebAdapter

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


class FailingProvider(FakeProvider):
    """Parks at the gate like FakeProvider, then the model call fails."""

    async def stream(
        self, *, model: str, messages: list[dict[str, Any]], tools: list[ToolSpec]
    ) -> AsyncIterator[ProviderEvent]:
        self.calls.append([dict(m) for m in messages])
        self.entered.set()
        assert self.gate is not None
        await self.gate.wait()
        raise ProviderError("proxy down")
        yield  # pragma: no cover - unreachable, keeps this an async generator


def manager_for(
    provider: FakeProvider, store: MessageStore, registry: ToolRegistry | None = None
) -> SessionManager:
    tools = registry or ToolRegistry()
    return SessionManager(
        provider=provider, tools_factory=lambda thread, channel: tools,
        store=store, default_model="m", system_prompt="s", max_concurrent=4,
        soul_reader=lambda: "",
    )


async def noop_delta(text: str) -> None:
    pass


def item(text: str, log: list[str]) -> Injected:
    async def consumed() -> None:
        log.append(f"consumed:{text}")

    return Injected(text, consumed, lambda: log.append(f"leftover:{text}"))


def open_inbox(*texts: str) -> tuple[Inbox, list[str]]:
    inbox, log = Inbox(), list[str]()
    inbox.open()
    for text in texts:
        inbox.offer(item(text, log))
    return inbox, log


# ---- inbox ----


async def test_closed_inbox_refuses_offers() -> None:
    log: list[str] = []
    assert Inbox().offer(item("hi", log)) is False
    assert log == []


async def test_inbox_take_drains_in_order_and_close_requeues_the_rest() -> None:
    inbox, log = open_inbox("a", "b")
    assert inbox.pending()
    assert await inbox.take() == ["a", "b"]
    assert not inbox.pending()
    inbox.offer(item("c", log))
    inbox.close()
    assert log == ["consumed:a", "consumed:b", "leftover:c"]
    assert inbox.offer(item("d", log)) is False


async def test_a_raising_consume_hook_leaves_the_rest_for_close() -> None:
    inbox, log = Inbox(), list[str]()
    inbox.open()

    async def boom() -> None:
        raise RuntimeError("hook failed")

    inbox.offer(Injected("bad", boom, lambda: log.append("leftover:bad")))
    inbox.offer(item("good", log))
    try:
        await inbox.take()
    except RuntimeError:
        pass
    inbox.close()
    assert log == ["leftover:good"]


# ---- loop ----


async def test_loop_appends_injected_text_before_the_next_model_call() -> None:
    gate = Gate()
    gate.release.set()
    provider = FakeProvider([tool_turn("wait", {}), text_turn("saw it")])
    inbox, _ = open_inbox("stop, do X instead")
    committed: list[dict[str, Any]] = []

    async def commit(message: dict[str, Any]) -> None:
        committed.append(message)

    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    result = await run_turn(
        provider=provider, model="m", messages=messages, tools=gate.registry(),
        on_delta=noop_delta, on_commit=commit, injections=inbox,
    )
    assert result.text == "saw it"
    injected = {"role": "user", "content": "stop, do X instead"}
    # After the tool result, before the second model call — and persisted.
    assert [m["role"] for m in messages] == [
        "user", "assistant", "tool", "user", "assistant"
    ]
    assert provider.calls[1][-1] == injected
    assert injected in committed


async def test_a_text_answer_overtaken_by_an_injection_goes_out_then_continues() -> (
    None
):
    """The owner spoke before the answer reached them: deliver the answer as
    an interim reply and answer the new text in the same turn, in order."""
    provider = FakeProvider([text_turn("first answer"), text_turn("second answer")])
    inbox, log = open_inbox("one more thing")
    interim: list[str] = []

    async def on_interim(text: str) -> None:
        interim.append(text)

    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    result = await run_turn(
        provider=provider, model="m", messages=messages, tools=ToolRegistry(),
        on_delta=noop_delta, injections=inbox, on_interim=on_interim,
    )
    assert interim == ["first answer"]
    assert result.text == "second answer"
    assert provider.calls[1][-1] == {"role": "user", "content": "one more thing"}
    assert log == ["consumed:one more thing"]


async def test_the_last_iteration_leaves_injections_for_requeue() -> None:
    """At the iteration cap the model can't see the text: don't take it (and
    announce it as seen) — leave it for the inbox's close to hand back."""
    gate = Gate()
    gate.release.set()
    provider = FakeProvider([tool_turn("wait", {})])
    inbox, log = open_inbox("too late")
    result = await run_turn(
        provider=provider, model="m", messages=[{"role": "user", "content": "go"}],
        tools=gate.registry(), on_delta=noop_delta, injections=inbox,
        max_iterations=1,
    )
    assert "iteration limit" in result.text
    assert inbox.pending()
    inbox.close()
    assert log == ["leftover:too late"]


# ---- session ----


async def test_session_injects_into_a_running_turn(store: MessageStore) -> None:
    gate = Gate()
    provider = FakeProvider([tool_turn("wait", {}), text_turn("ok, switching")])
    manager = manager_for(provider, store, gate.registry())
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


async def test_session_requeues_an_injection_when_the_turn_fails(
    store: MessageStore,
) -> None:
    provider = FailingProvider([])
    provider.gate = asyncio.Event()
    session = await manager_for(provider, store).get_or_create("cli:t", "cli")
    log: list[str] = []
    turn = asyncio.create_task(session.run_turn("start", noop_delta))
    await provider.entered.wait()
    assert session.inject(item("still there?", log)) is True
    provider.gate.set()
    try:
        await turn
    except ProviderError:
        pass
    assert log == ["leftover:still there?"]


async def test_on_begin_fires_once_the_turn_holds_the_lock(
    store: MessageStore,
) -> None:
    provider = FakeProvider([text_turn("one"), text_turn("two")])
    session = await manager_for(provider, store).get_or_create("cli:t", "cli")
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
    manager = manager_for(FakeProvider([]), store)
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
    provider = FakeProvider([tool_turn("wait", {}), text_turn("noted")])
    hub, bus = ObserverHub(), EventBus()
    seen: list[Event] = []

    async def record(event: Event) -> None:
        seen.append(event)

    bus.subscribe(record)
    dispatcher = Dispatcher(
        manager_for(provider, store, gate.registry()), hub=hub, bus=bus
    )
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


async def test_an_overtaken_answer_is_sent_before_the_final(
    store: MessageStore,
) -> None:
    provider = FakeProvider([text_turn("answer one"), text_turn("answer two")])
    provider.gate = asyncio.Event()
    dispatcher = Dispatcher(manager_for(provider, store))
    adapter = RecordingAdapter()
    dispatcher.register(adapter)
    turn = asyncio.create_task(dispatcher.handle(owner("start")))
    await provider.entered.wait()
    requeued: list[Message] = []
    assert dispatcher.try_inject(owner("wait, also"), requeued.append) is True
    provider.gate.set()
    await turn
    assert adapter.sent == [("cli:t", "answer one"), ("cli:t", "answer two")]
    assert requeued == []


async def test_leftover_injection_is_requeued_through_the_callback(
    store: MessageStore,
) -> None:
    provider = FailingProvider([])
    provider.gate = asyncio.Event()
    dispatcher = Dispatcher(manager_for(provider, store))
    dispatcher.register(RecordingAdapter())
    turn = asyncio.create_task(dispatcher.handle(owner("start")))
    await provider.entered.wait()
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
    hub = ObserverHub()
    manager = manager_for(FakeProvider([text_turn("hey")]), store)
    dispatcher = Dispatcher(manager, hub=hub)
    dispatcher.register(_WebAdapter())
    queue = hub.listen("web:main")
    await dispatcher.handle(
        Message(channel="web", sender="owner", thread_key="web:main", text="hi")
    )
    assert queue.get_nowait() == {"type": "inbound", "thread": "web:main", "text": "hi"}
