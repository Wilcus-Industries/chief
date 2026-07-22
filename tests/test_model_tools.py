"""The `switch_model` native tool: approval-gated per-thread model switch."""

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.agent.manager import SessionManager
from chief.agent.model_tools import register_switch_model_tool
from chief.persistence.store import MessageStore
from chief.provider.base import ToolCall
from chief.tools import ToolContext, ToolRegistry

from .fakes import FakeProvider

CTX = ToolContext(thread_key="cli:t", channel="cli")


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
    register_switch_model_tool(registry, manager, model_aliases=frozenset({"opus"}))
    return registry, manager


def switch_call(model: str) -> ToolCall:
    return ToolCall(id="1", name="switch_model", arguments={"model": model})


def test_switch_model_is_gray_not_read_only(store: MessageStore) -> None:
    registry, _ = make_registry(store)
    spec = next(s for s in registry.specs() if s.name == "switch_model")
    # Not read_only -> the gate raises an approval card (never auto-approved).
    assert spec.read_only is False


async def test_switch_model_sets_the_thread_override(
    engine: AsyncEngine, store: MessageStore
) -> None:
    registry, _ = make_registry(store)
    result = await registry.dispatch(switch_call("opus"), context=CTX)
    assert "opus" in result
    assert "next turn" in result
    assert await store.model_override("cli:t") == "opus"


async def test_switch_model_rejects_a_name_that_routes_nowhere(
    engine: AsyncEngine, store: MessageStore
) -> None:
    """Same wedge as `/model`, reachable by the agent instead of the owner."""
    registry, _ = make_registry(store)
    result = await registry.dispatch(switch_call("sonnet"), context=CTX)
    assert result.startswith("error:")
    assert "opus" in result
    assert await store.model_override("cli:t") is None, "must not persist"


async def test_switch_model_needs_a_session_context(store: MessageStore) -> None:
    registry, _ = make_registry(store)
    result = await registry.dispatch(switch_call("opus"), context=None)
    assert "context" in result
