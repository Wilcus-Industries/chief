"""Slash commands: deterministic control plane, parsed before any model call."""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Message
from chief.agent.compaction import NOTE_PREFIX, Compactor
from chief.agent.manager import SessionManager
from chief.bus import EventBus
from chief.classifiers import Classifier, ClassifierRegistry
from chief.commands import CommandSet
from chief.cron.service import CronService
from chief.dispatch import Dispatcher
from chief.monitors.service import MonitorService
from chief.persistence.db import SessionFactory, make_session_factory
from chief.persistence.store import MessageStore
from chief.tools import ToolRegistry

from .fakes import FakeProvider, text_turn
from .test_compaction import FixedWindow, chat
from .test_dispatch import RecordingAdapter


def make_commands_with_compactor(
    provider: FakeProvider, store: MessageStore, factory: SessionFactory
) -> tuple[CommandSet, SessionManager]:
    """Like ``make_commands`` but wires a force-only Compactor (huge window, so
    only ``/compact`` ever fires) — for exercising the /compact handler."""
    manager = SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="default-model",
        system_prompt="s",
        max_concurrent=4,
        compactor=Compactor(provider, "m", FixedWindow(10_000_000), keep_recent=4),
    )
    monitors = MonitorService(
        factory,
        EventBus(),
        _no_wake,
        Classifier(FakeProvider([]), ClassifierRegistry(Path("classifiers")), "m"),
    )
    commands = CommandSet(
        manager, monitors, CronService(factory, _no_wake), store=store
    )
    return commands, manager


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
        factory,
        EventBus(),
        _no_wake,
        Classifier(FakeProvider([]), ClassifierRegistry(Path("classifiers")), "m"),
    )
    cron = CronService(factory, _no_wake)
    commands = CommandSet(
        manager, monitors, cron, store=store, model_aliases=frozenset({"opus"})
    )
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
    assert isinstance(reply, str)
    for name in ("/help", "/monitors", "/schedules", "/model", "/compact"):
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
    assert isinstance(monitors_reply, str) and "urgent watcher" in monitors_reply
    assert isinstance(schedules_reply, str) and "daily checkin" in schedules_reply


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


async def test_model_command_rejects_a_name_that_routes_nowhere(
    engine: AsyncEngine, store: MessageStore
) -> None:
    """The live bug: `/model sonnet` with no sonnet alias wedged the thread.

    It reported success, persisted the override, and then every turn in that
    thread died on `OpenRouter HTTP 400: sonnet is not a valid model ID`.
    """
    factory = make_session_factory(engine)
    commands, manager, *_ = make_commands(FakeProvider([]), store, factory)

    reply = await commands.run(msg("/model sonnet"))

    assert isinstance(reply, str)
    assert reply.startswith("error:")
    assert "opus" in reply, "the error must name a model that does work"
    session = await manager.get_or_create("cli:t", "cli")
    assert session.model == "default-model", "a rejected name must not persist"


async def test_model_command_accepts_a_configured_alias(
    engine: AsyncEngine, store: MessageStore
) -> None:
    factory = make_session_factory(engine)
    commands, manager, *_ = make_commands(FakeProvider([]), store, factory)
    assert await commands.run(msg("/model opus")) == "model set to opus for this thread"
    session = await manager.get_or_create("cli:t", "cli")
    assert session.model == "opus"


async def test_clear_wipes_transcript_and_resets_live_session(
    engine: AsyncEngine, store: MessageStore
) -> None:
    factory = make_session_factory(engine)
    commands, manager, *_ = make_commands(FakeProvider([]), store, factory)
    await store.ensure_session("cli:t", "cli")
    await store.append("cli:t", [{"role": "user", "content": "hi"}])
    session = await manager.get_or_create("cli:t", "cli")  # cache it live

    reply = await commands.run(msg("/clear"))

    assert isinstance(reply, str) and "clear" in reply.lower()
    assert await store.load("cli:t") == []  # transcript gone, row kept
    assert await store.list_sessions()  # session row survives
    # the live session is dropped so the next turn reloads empty history
    assert await manager.get_or_create("cli:t", "cli") is not session


async def test_prune_deletes_web_scratch_buffers_only(
    engine: AsyncEngine, store: MessageStore
) -> None:
    factory = make_session_factory(engine)
    commands, manager, *_ = make_commands(FakeProvider([]), store, factory)
    for thread_key, channel in (
        ("web:main", "web"),
        ("web:scratch", "web"),
        ("web:notes", "web"),
        ("imessage:+1", "imessage"),
        ("cli:t", "cli"),
    ):
        await store.ensure_session(thread_key, channel)
    scratch = await manager.get_or_create("web:scratch", "web")  # cache it live

    reply = await commands.run(msg("/prune"))

    assert isinstance(reply, str) and "2" in reply
    threads = {s["thread"] for s in await store.list_sessions()}
    assert threads == {"web:main", "imessage:+1", "cli:t"}
    # a live scratch session is dropped along with its row
    assert await manager.get_or_create("web:scratch", "web") is not scratch


async def test_prune_skips_a_busy_buffer(
    engine: AsyncEngine, store: MessageStore
) -> None:
    factory = make_session_factory(engine)
    commands, manager, *_ = make_commands(FakeProvider([]), store, factory)
    for thread_key in ("web:scratch", "web:busy"):
        await store.ensure_session(thread_key, "web")
    busy = await manager.get_or_create("web:busy", "web")

    async with busy.lock:  # web:busy is mid-turn — prune must leave it
        reply = await commands.run(msg("/prune"))

    assert isinstance(reply, str)
    assert "pruned 1" in reply and "skipped 1" in reply
    threads = {s["thread"] for s in await store.list_sessions()}
    assert "web:busy" in threads
    assert "web:scratch" not in threads


async def test_compact_forces_this_thread(
    engine: AsyncEngine, store: MessageStore
) -> None:
    factory = make_session_factory(engine)
    provider = FakeProvider([text_turn("the note")])
    commands, manager = make_commands_with_compactor(provider, store, factory)
    history = chat(20)
    await store.ensure_session("cli:t", "cli")
    await store.append("cli:t", history)
    session = await manager.get_or_create("cli:t", "cli")  # cache it live

    reply = await commands.run(msg("/compact"))

    assert isinstance(reply, str) and reply.startswith("compacted")
    persisted = await store.load("cli:t")
    assert persisted[0]["content"].startswith(NOTE_PREFIX + "the note")
    # the live session was folded too, not just the DB
    assert session._messages[0]["content"].startswith(NOTE_PREFIX + "the note")


async def test_compact_targets_a_named_thread(
    engine: AsyncEngine, store: MessageStore
) -> None:
    factory = make_session_factory(engine)
    provider = FakeProvider([text_turn("group note")])
    commands, manager = make_commands_with_compactor(provider, store, factory)
    await store.ensure_session("+15551234567", "imessage")
    await store.append("+15551234567", chat(20))

    # arrives on a cli: socket thread but names the imessage self-chat target
    reply = await commands.run(msg("/compact +15551234567"))

    assert isinstance(reply, str) and reply.startswith("compacted")
    persisted = await store.load("+15551234567")
    assert persisted[0]["content"].startswith(NOTE_PREFIX + "group note")


async def test_compact_unknown_thread_reports_cleanly(
    engine: AsyncEngine, store: MessageStore
) -> None:
    commands, _ = make_commands_with_compactor(
        FakeProvider([]), store, make_session_factory(engine)
    )
    assert await commands.run(msg("/compact no:such")) == "no such thread: no:such"


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
