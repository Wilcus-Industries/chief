"""Dispatcher routing: approvals first, strangers dropped, bus publishing."""

import asyncio
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Adapter, Message
from chief.agent.manager import SessionManager
from chief.agent.tools import Tool, ToolRegistry
from chief.approvals import Approval, ApprovalBroker
from chief.bus import Event, EventBus
from chief.dispatch import Dispatcher
from chief.persistence.db import make_session_factory
from chief.persistence.store import MessageStore
from chief.provider.base import ProviderError, ProviderEvent, ToolSpec
from chief.selfedit.recovery import RestartController
from chief.strangers import StrangerLog

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
    assert [e.payload["text"] for e in seen] == ["from owner"]
    assert len(provider.calls) == 2  # both still ran turns
