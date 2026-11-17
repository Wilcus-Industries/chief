"""The `session` native tool: the agent listing/creating/deleting its threads."""

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.agent.manager import SessionManager
from chief.agent.session_tools import register_session_tools
from chief.persistence.store import MessageStore
from chief.provider.base import ToolCall
from chief.tools import ToolContext, ToolRegistry

from .fakes import FakeProvider


def make_registry(
    store: MessageStore,
    owner_handles: tuple[str, ...] = (),
    primary: tuple[str, str] | None = None,
) -> tuple[ToolRegistry, SessionManager]:
    manager = SessionManager(
        provider=FakeProvider([]),
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="default-model",
        system_prompt="s",
        max_concurrent=4,
    )
    registry = ToolRegistry()
    register_session_tools(
        registry, manager, owner_handles=owner_handles, primary=primary
    )
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


async def test_list_tags_threads_that_reach_only_the_owner(
    engine: AsyncEngine, store: MessageStore
) -> None:
    """A session that must reach the owner (the forward-to-owner rule in the
    messaging skill) finds the owner's iMessage thread by this tag; a
    stranger's 1:1 on the same channel stays untagged."""
    await store.ensure_session("+15550001", "imessage")
    await store.ensure_session("+15559999", "imessage")
    await store.ensure_session("web:main", "web")
    registry, _ = make_registry(store, owner_handles=("+15550001",))
    lines = (await registry.dispatch(call("list"), context=CTX)).splitlines()
    by_thread = {line.split()[1]: line for line in lines}
    assert "(owner)" in by_thread["+15550001"]
    assert "(owner)" in by_thread["web:main"]
    assert "(owner)" not in by_thread["+15559999"]
    assert "primary" not in "".join(lines)


async def test_list_tags_the_primary_owner_thread(
    engine: AsyncEngine, store: MessageStore
) -> None:
    """With several owner threads (phone and email handles), exactly one —
    ``primary_target`` — is where a forward to the owner goes."""
    await store.ensure_session("+15550001", "imessage")
    await store.ensure_session("owner@example.com", "imessage")
    registry, _ = make_registry(
        store,
        owner_handles=("+15550001", "owner@example.com"),
        primary=("imessage", "+15550001"),
    )
    lines = (await registry.dispatch(call("list"), context=CTX)).splitlines()
    by_thread = {line.split()[1]: line for line in lines}
    assert "(owner, primary)" in by_thread["+15550001"]
    assert "primary" not in by_thread["owner@example.com"]
    assert "(owner)" in by_thread["owner@example.com"]


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


async def test_create_with_policy_persists_a_normalized_override(
    engine: AsyncEngine, store: MessageStore
) -> None:
    """AC4: a `policy` arg on create stores the full normalized policy dict."""
    registry, _ = make_registry(store)
    await registry.dispatch(
        call(
            "create",
            thread_key="cli:new",
            policy={"deltas": True, "tools": True, "results": "inline"},
        ),
        context=CTX,
    )
    assert await store.stream_policy("cli:new") == {
        "deltas": True, "tools": True, "results": "inline", "send_guard": False
    }


async def test_create_on_existing_thread_reports_unchanged(
    engine: AsyncEngine, store: MessageStore
) -> None:
    """Re-creating a known thread without a policy is honest about the no-op."""
    await store.ensure_session("cli:new", "cli")
    registry, _ = make_registry(store)
    result = await registry.dispatch(
        call("create", thread_key="cli:new"), context=CTX
    )
    assert "already exists" in result
    assert "registered" not in result


async def test_create_with_policy_on_existing_thread_updates_override(
    engine: AsyncEngine, store: MessageStore
) -> None:
    """A policy on an already-existing thread is applied, not silently dropped."""
    await store.ensure_session("cli:new", "cli")
    registry, _ = make_registry(store)
    result = await registry.dispatch(
        call(
            "create",
            thread_key="cli:new",
            policy={"deltas": True, "tools": True, "results": "inline"},
        ),
        context=CTX,
    )
    assert "already exists" in result
    assert await store.stream_policy("cli:new") == {
        "deltas": True, "tools": True, "results": "inline", "send_guard": False
    }


async def test_create_with_a_bad_policy_errors_and_creates_no_override(
    engine: AsyncEngine, store: MessageStore
) -> None:
    registry, _ = make_registry(store)
    result = await registry.dispatch(
        call("create", thread_key="cli:new", policy={"results": "nope"}),
        context=CTX,
    )
    assert result.startswith("error: invalid stream policy")
    assert await store.channel("cli:new") is None  # no row created


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
