"""Dispatcher routing: approvals first, strangers dropped, bus publishing."""

import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Adapter, Message
from chief.agent.manager import SessionManager
from chief.agent.tools import ToolRegistry
from chief.approvals import ApprovalBroker
from chief.bus import Event, EventBus
from chief.dispatch import Dispatcher
from chief.persistence.db import make_session_factory
from chief.persistence.store import MessageStore
from chief.strangers import StrangerLog

from .fakes import FakeProvider, text_turn


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
    assert await card is True
    assert provider.calls == []  # the yes never became a turn


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
