"""The `session` native tool: the agent listing/creating/deleting its threads."""

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.agent.manager import SessionManager
from chief.agent.session_tools import register_session_tools
from chief.agent.tools import ToolContext, ToolRegistry
from chief.persistence.store import MessageStore
from chief.provider.base import ToolCall

from .fakes import FakeProvider


def make_registry(store: MessageStore) -> tuple[ToolRegistry, SessionManager]:
    manager = SessionManager(
        provider=FakeProvider([]),
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="default-model",
        system_prompt="s",
        max_concurrent=4,
    )
    registry = ToolRegistry()
    register_session_tools(registry, manager)
    return registry, manager


def call(action: str, **args: object) -> ToolCall:
    return ToolCall(id="1", name="session", arguments={"action": action, **args})


CTX = ToolContext(thread_key="cli:self", channel="cli")


async def test_list_reports_known_threads(
    engine: AsyncEngine, store: MessageStore
) -> None:
    await store.ensure_session("imessage:+1", "imessage")
    registry, _ = make_registry(store)
    result = await registry.dispatch(call("list"), context=CTX)
    assert "imessage:+1" in result
    assert "imessage" in result


async def test_list_when_empty(engine: AsyncEngine, store: MessageStore) -> None:
    registry, _ = make_registry(store)
    assert await registry.dispatch(call("list"), context=CTX) == "no sessions"


async def test_create_registers_a_thread(
    engine: AsyncEngine, store: MessageStore
) -> None:
    registry, _ = make_registry(store)
    result = await registry.dispatch(
        call("create", thread_key="cli:new", channel="cli"), context=CTX
    )
    assert "cli:new" in result
    assert await store.channel("cli:new") == "cli"


async def test_create_defaults_channel_to_context(
    engine: AsyncEngine, store: MessageStore
) -> None:
    registry, _ = make_registry(store)
    await registry.dispatch(call("create", thread_key="cli:new"), context=CTX)
    assert await store.channel("cli:new") == "cli"


async def test_delete_removes_a_thread(
    engine: AsyncEngine, store: MessageStore
) -> None:
    await store.ensure_session("cli:other", "cli")
    registry, _ = make_registry(store)
    result = await registry.dispatch(
        call("delete", thread_key="cli:other"), context=CTX
    )
    assert "cli:other" in result
    assert await store.channel("cli:other") is None


async def test_clear_wipes_transcript_keeps_row(
    engine: AsyncEngine, store: MessageStore
) -> None:
    await store.ensure_session("cli:other", "cli")
    await store.append("cli:other", [{"role": "user", "content": "hi"}])
    registry, _ = make_registry(store)
    await registry.dispatch(call("clear", thread_key="cli:other"), context=CTX)
    assert await store.load("cli:other") == []
    assert await store.channel("cli:other") == "cli"


async def test_delete_own_thread_refused(
    engine: AsyncEngine, store: MessageStore
) -> None:
    await store.ensure_session("cli:self", "cli")
    registry, _ = make_registry(store)
    result = await registry.dispatch(
        call("delete", thread_key="cli:self"), context=CTX
    )
    assert result.startswith("error")
    assert await store.channel("cli:self") == "cli"


async def test_clear_own_thread_refused(
    engine: AsyncEngine, store: MessageStore
) -> None:
    await store.ensure_session("cli:self", "cli")
    await store.append("cli:self", [{"role": "user", "content": "hi"}])
    registry, _ = make_registry(store)
    result = await registry.dispatch(
        call("clear", thread_key="cli:self"), context=CTX
    )
    assert result.startswith("error")
    assert await store.load("cli:self") == [{"role": "user", "content": "hi"}]


async def test_delete_busy_other_thread_refused(
    engine: AsyncEngine, store: MessageStore
) -> None:
    await store.ensure_session("cli:busy", "cli")
    await store.append("cli:busy", [{"role": "user", "content": "hi"}])
    registry, manager = make_registry(store)
    session = await manager.get_or_create("cli:busy", "cli")
    async with session.lock:  # a turn holds this lock for its whole duration
        result = await registry.dispatch(
            call("delete", thread_key="cli:busy"), context=CTX
        )
    assert result.startswith("error")
    assert await store.channel("cli:busy") == "cli"
    assert await store.load("cli:busy") == [{"role": "user", "content": "hi"}]


async def test_clear_busy_other_thread_refused(
    engine: AsyncEngine, store: MessageStore
) -> None:
    await store.ensure_session("cli:busy", "cli")
    await store.append("cli:busy", [{"role": "user", "content": "hi"}])
    registry, manager = make_registry(store)
    session = await manager.get_or_create("cli:busy", "cli")
    async with session.lock:  # a turn holds this lock for its whole duration
        result = await registry.dispatch(
            call("clear", thread_key="cli:busy"), context=CTX
        )
    assert result.startswith("error")
    assert await store.load("cli:busy") == [{"role": "user", "content": "hi"}]


async def test_delete_requires_thread_key(
    engine: AsyncEngine, store: MessageStore
) -> None:
    registry, _ = make_registry(store)
    result = await registry.dispatch(call("delete"), context=CTX)
    assert result.startswith("error")


async def test_unknown_action_errors(
    engine: AsyncEngine, store: MessageStore
) -> None:
    registry, _ = make_registry(store)
    result = await registry.dispatch(call("frobnicate"), context=CTX)
    assert result.startswith("error")
