"""Slash commands: deterministic control plane, parsed before any model call."""

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Message
from chief.agent.manager import SessionManager
from chief.agent.tools import ToolRegistry
from chief.bus import EventBus
from chief.commands import CommandSet
from chief.cron.service import CronService
from chief.dispatch import Dispatcher
from chief.monitors.service import ModelJudge, MonitorService
from chief.persistence.db import SessionFactory, make_session_factory
from chief.persistence.store import MessageStore

from .fakes import FakeProvider, text_turn
from .test_dispatch import RecordingAdapter


def make_commands(
    provider: FakeProvider, store: MessageStore, factory: SessionFactory
) -> tuple[CommandSet, SessionManager, MonitorService, CronService]:
    manager = SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="default-model",
        system_prompt="s",
        max_concurrent=4,
    )
    monitors = MonitorService(
        factory, EventBus(), _no_wake, ModelJudge(FakeProvider([]), "m")
    )
    cron = CronService(factory, _no_wake)
    commands = CommandSet(manager, monitors, cron)
    return commands, manager, monitors, cron


async def _no_wake(message: Message) -> None:
    raise AssertionError("unexpected wake")


def msg(text: str) -> Message:
    return Message(channel="cli", sender="owner", thread_key="cli:t", text=text)


async def test_non_command_returns_none(
    engine: AsyncEngine, store: MessageStore
) -> None:
    commands, *_ = make_commands(FakeProvider([]), store, make_session_factory(engine))
    assert await commands.run(msg("hello there")) is None


async def test_unknown_command_points_to_help(
    engine: AsyncEngine, store: MessageStore
) -> None:
    commands, *_ = make_commands(FakeProvider([]), store, make_session_factory(engine))
    assert await commands.run(msg("/bogus")) == "unknown command /bogus — try /help"


async def test_help_lists_commands(engine: AsyncEngine, store: MessageStore) -> None:
    commands, *_ = make_commands(FakeProvider([]), store, make_session_factory(engine))
    reply = await commands.run(msg("/help"))
    assert reply is not None
    for name in ("/help", "/monitors", "/schedules", "/model"):
        assert name in reply


async def test_monitors_and_schedules_listing(
    engine: AsyncEngine, store: MessageStore
) -> None:
    factory = make_session_factory(engine)
    commands, _, monitors, cron = make_commands(FakeProvider([]), store, factory)
    assert await commands.run(msg("/monitors")) == "no monitors"
    assert await commands.run(msg("/schedules")) == "no schedules"
    await monitors.create(
        description="urgent watcher",
        watch_channel="cli",
        wake_channel="cli",
        wake_thread="cli:t",
        predicate={"kind": "code", "field": "text", "pattern": "x"},
    )
    await cron.create(
        description="daily checkin",
        spec="0 9 * * *",
        wake_channel="cli",
        wake_thread="cli:t",
        prompt="hi",
    )
    monitors_reply = await commands.run(msg("/monitors"))
    schedules_reply = await commands.run(msg("/schedules"))
    assert monitors_reply is not None and "urgent watcher" in monitors_reply
    assert schedules_reply is not None and "daily checkin" in schedules_reply


async def test_model_command_shows_and_sets_with_persistence(
    engine: AsyncEngine, store: MessageStore
) -> None:
    factory = make_session_factory(engine)
    provider = FakeProvider([text_turn("ok")])
    commands, manager, *_ = make_commands(provider, store, factory)
    assert await commands.run(msg("/model")) == "model: default-model"
    reply = await commands.run(msg("/model big/model"))
    assert reply == "model set to big/model for this thread"
    session = await manager.get_or_create("cli:t", "cli")
    assert session.model == "big/model"

    # A fresh manager (daemon restart) resumes the override from disk.
    commands2, manager2, *_ = make_commands(provider, store, factory)
    resumed = await manager2.get_or_create("cli:t", "cli")
    assert resumed.model == "big/model"


async def test_dispatcher_answers_commands_without_a_turn(
    engine: AsyncEngine, store: MessageStore
) -> None:
    provider = FakeProvider([])
    commands, manager, *_ = make_commands(
        provider, store, make_session_factory(engine)
    )
    dispatcher = Dispatcher(manager)
    dispatcher.set_commands(commands)
    adapter = RecordingAdapter()
    dispatcher.register(adapter)
    await dispatcher.handle(msg("/help"))
    assert provider.calls == []
    assert len(adapter.sent) == 1
    assert "/monitors" in adapter.sent[0][1]
