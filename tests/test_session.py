"""Sessions: serial turn queue, persistence, restart resume, concurrency cap."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from chief.agent.manager import SessionManager
from chief.persistence.store import MessageStore
from chief.provider.base import ProviderEvent, ToolSpec
from chief.selfedit.restart import RestartController
from chief.tools import Tool, ToolRegistry

from .fakes import FakeProvider, text_turn, tool_turn


class _RaisingProvider:
    """Raises mid-stream, after recording the call — simulates a provider or
    network failure once the turn is already underway (#261's atomic-
    persistence finding)."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(
        self, *, model: str, messages: list[dict[str, Any]], tools: list[ToolSpec]
    ) -> AsyncIterator[ProviderEvent]:
        self.calls.append([dict(m) for m in messages])
        raise RuntimeError("provider connection dropped")
        yield  # pragma: no cover - unreachable, keeps this an async generator


async def noop_delta(text: str) -> None:
    pass


def make_manager(
    provider: FakeProvider,
    store: MessageStore,
    max_concurrent: int = 4,
    soul_reader: Any = None,
) -> SessionManager:
    return SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="test-model",
        system_prompt="test system prompt",
        max_concurrent=max_concurrent,
        # Default to no soul so prompt assertions don't depend on a real
        # data/memory/Soul.md under the test's cwd.
        soul_reader=soul_reader or (lambda: ""),
    )


async def test_turn_persists_transcript_and_injects_system_prompt(
    store: MessageStore,
) -> None:
    provider = FakeProvider([text_turn("hi there")])
    manager = make_manager(provider, store)
    session = await manager.get_or_create("cli:t", "cli")
    result = await session.run_turn("hello", noop_delta)
    assert result.text == "hi there"
    system = provider.calls[0][0]
    assert system["role"] == "system"
    # The prompt is injected, and names the thread's origin channel so the
    # agent knows which device it's speaking through.
    assert system["content"].startswith("test system prompt")
    assert "over the 'cli' channel" in system["content"]
    saved = await store.load("cli:t")
    assert saved == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


async def test_soul_leads_the_prompt_and_is_read_each_turn(
    store: MessageStore,
) -> None:
    # The soul is inlined at the very top, ahead of the base prompt and the
    # origin-channel note, and re-read every turn so edits apply immediately.
    souls = iter(["I am v1.", "I am v2."])
    provider = FakeProvider([text_turn("a"), text_turn("b")])
    manager = make_manager(provider, store, soul_reader=lambda: next(souls))
    session = await manager.get_or_create("cli:t", "cli")

    await session.run_turn("one", noop_delta)
    first = provider.calls[0][0]["content"]
    assert first.startswith("I am v1.\n\ntest system prompt")
    assert "over the 'cli' channel" in first

    await session.run_turn("two", noop_delta)
    second = provider.calls[1][0]["content"]
    assert second.startswith("I am v2.\n\ntest system prompt")


async def test_restart_resumes_history_from_disk(store: MessageStore) -> None:
    provider = FakeProvider([text_turn("first reply")])
    manager = make_manager(provider, store)
    session = await manager.get_or_create("cli:t", "cli")
    await session.run_turn("first", noop_delta)

    # A fresh manager over the same store simulates a daemon restart.
    provider2 = FakeProvider([text_turn("second reply")])
    manager2 = make_manager(provider2, store)
    resumed = await manager2.get_or_create("cli:t", "cli")
    await resumed.run_turn("second", noop_delta)
    sent = provider2.calls[0]
    assert sent[1] == {"role": "user", "content": "first"}
    assert sent[2] == {"role": "assistant", "content": "first reply"}
    assert sent[3] == {"role": "user", "content": "second"}


async def test_same_thread_turns_run_strictly_in_order(store: MessageStore) -> None:
    provider = FakeProvider([text_turn("reply one"), text_turn("reply two")])
    manager = make_manager(provider, store)
    session = await manager.get_or_create("cli:t", "cli")
    await asyncio.gather(
        session.run_turn("one", noop_delta), session.run_turn("two", noop_delta)
    )
    # The second model call must already contain the whole first exchange.
    assert {"role": "assistant", "content": "reply one"} in provider.calls[1]


async def test_semaphore_caps_concurrent_turns_across_threads(
    store: MessageStore,
) -> None:
    provider = FakeProvider([text_turn("a"), text_turn("b")])
    provider.gate = asyncio.Event()
    manager = make_manager(provider, store, max_concurrent=1)
    first = await manager.get_or_create("cli:one", "cli")
    second = await manager.get_or_create("cli:two", "cli")
    task_one = asyncio.create_task(first.run_turn("x", noop_delta))
    task_two = asyncio.create_task(second.run_turn("y", noop_delta))
    await asyncio.sleep(0.05)
    # Only one turn may reach the provider while the cap is 1 and it's blocked.
    assert len(provider.calls) == 1
    provider.gate.set()
    await asyncio.gather(task_one, task_two)
    assert len(provider.calls) == 2


async def test_tool_call_message_persists_before_its_result(
    store: MessageStore,
) -> None:
    """Each turn message lands in the store as it's produced, not batched at
    the end — so a concurrent reader (the web history-tool route, #261) sees
    the assistant's tool_calls message committed while the tool is still
    running, with only that call's result missing."""
    gate = asyncio.Event()

    async def slow_tool() -> str:
        await gate.wait()
        return "the slow result"

    registry = ToolRegistry()
    registry.register(
        Tool(ToolSpec(name="slow", description="", parameters={}), slow_tool)
    )
    provider = FakeProvider([tool_turn("slow", {}), text_turn("done")])
    manager = SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: registry,
        store=store,
        default_model="test-model",
        system_prompt="s",
        max_concurrent=4,
    )
    session = await manager.get_or_create("cli:t", "cli")
    task = asyncio.create_task(session.run_turn("go slow", noop_delta))
    await asyncio.sleep(0.05)  # let the loop commit the call and start dispatch

    mid_turn = await store.load("cli:t")
    assert mid_turn[-1]["role"] == "assistant"
    assert mid_turn[-1]["tool_calls"][0]["function"]["name"] == "slow"
    assert not any(m["role"] == "tool" for m in mid_turn)

    gate.set()
    await task
    finished = await store.load("cli:t")
    assert any(
        m["role"] == "tool" and m["content"] == "the slow result" for m in finished
    )


async def test_resume_repairs_a_dangling_tool_call_from_a_crash(
    store: MessageStore,
) -> None:
    """A process death between committing the assistant's tool_calls message
    and its result would otherwise leave that call's result missing forever:
    the transcript view would report it pending indefinitely, and the
    provider requires every tool_call answered before the next turn. On
    resume the manager closes it out with an error result instead."""
    await store.ensure_session("cli:t", "cli")
    await store.append(
        "cli:t",
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "slow", "arguments": "{}"},
                    }
                ],
            },
        ],
    )
    manager = make_manager(FakeProvider([]), store)
    session = await manager.get_or_create("cli:t", "cli")

    repaired = {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "error: interrupted before this tool call finished "
        "(process restarted)",
    }
    assert session._messages[-1] == repaired
    assert (await store.load("cli:t"))[-1] == repaired  # persisted, not just in memory


async def test_resume_repairs_a_parallel_call_crashed_mid_results(
    store: MessageStore,
) -> None:
    """A parallel tool_calls message can crash after only some of its results
    land, leaving the tail on a tool-role message rather than the assistant
    one. Repair must still find the dangling call by scanning for the last
    assistant tool_calls message, not by checking history[-1]'s role (#261)."""
    await store.ensure_session("cli:t", "cli")
    await store.append(
        "cli:t",
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "slow", "arguments": "{}"},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "slow", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "done"},
        ],
    )
    manager = make_manager(FakeProvider([]), store)
    session = await manager.get_or_create("cli:t", "cli")

    repaired = {
        "role": "tool",
        "tool_call_id": "c2",
        "content": "error: interrupted before this tool call finished "
        "(process restarted)",
    }
    assert session._messages[-1] == repaired
    assert (await store.load("cli:t"))[-1] == repaired  # persisted, not just in memory
    # c1's real result must survive untouched.
    assert any(m == {"role": "tool", "tool_call_id": "c1", "content": "done"}
               for m in session._messages)


async def test_a_provider_crash_keeps_memory_in_sync_with_the_store(
    store: MessageStore,
) -> None:
    """The user message is persisted up front; if the provider then raises,
    in-memory history must still learn about it (and anything `_live_append`
    already committed) instead of silently orphaning the turn (#261)."""
    provider = _RaisingProvider()
    manager = make_manager(provider, store)  # type: ignore[arg-type]
    session = await manager.get_or_create("cli:t", "cli")

    with pytest.raises(RuntimeError):
        await session.run_turn("hello", noop_delta)

    expected = [{"role": "user", "content": "hello"}]
    assert session._messages == expected
    assert await store.load("cli:t") == expected


async def test_resume_leaves_a_complete_transcript_untouched(
    store: MessageStore,
) -> None:
    provider = FakeProvider([text_turn("hi there")])
    manager = make_manager(provider, store)
    session = await manager.get_or_create("cli:t", "cli")
    await session.run_turn("hello", noop_delta)

    resumed = await make_manager(FakeProvider([]), store).get_or_create("cli:t", "cli")
    assert resumed._messages == await store.load("cli:t")


async def test_run_turn_commits_but_does_not_fire_restart(
    store: MessageStore,
) -> None:
    """run_turn must commit the transcript but NOT execv: the restart fires at
    the outermost boundary (dispatcher / imessage poll) once the reply — and
    any inbound cursor — is durable. Firing inside the turn would preempt the
    un-sent reply and, on imessage, re-poll the row (the double-send bug)."""
    events: list[str] = []
    controller = RestartController(lambda: events.append("restart"))

    registry = ToolRegistry()

    async def fake_restart() -> str:
        controller.request()
        return "restarting into the new code"

    registry.register(
        Tool(ToolSpec(name="restart", description="", parameters={}), fake_restart)
    )

    original_append = store.append

    async def traced_append(thread_key: str, messages: list[dict[str, Any]]) -> None:
        events.append("commit")
        await original_append(thread_key, messages)

    store.append = traced_append  # type: ignore[method-assign]

    provider = FakeProvider([tool_turn("restart", {}), text_turn("done, back soon")])
    manager = SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: registry,
        store=store,
        default_model="test-model",
        system_prompt="test system prompt",
        max_concurrent=4,
        restart_gate=controller,
    )
    session = await manager.get_or_create("cli:t", "cli")
    await session.run_turn("install imessage", noop_delta)

    # Committed (now incrementally as each message lands — see #261's
    # mid-turn persistence — so several "commit"s, not one), but the restart
    # is only requested, never fired here.
    assert events and "restart" not in events
    saved = await store.load("cli:t")
    assert saved[0] == {"role": "user", "content": "install imessage"}
    assert saved[-1] == {"role": "assistant", "content": "done, back soon"}
    # The pending restart fires once the boundary owner calls it — strictly
    # after every commit above, never interleaved with them.
    await controller.fire_if_requested()
    assert events[-1] == "restart"
    assert events.count("restart") == 1


async def test_get_or_create_returns_the_same_session(store: MessageStore) -> None:
    provider = FakeProvider([])
    manager = make_manager(provider, store)
    a = await manager.get_or_create("cli:t", "cli")
    b = await manager.get_or_create("cli:t", "cli")
    assert a is b


async def test_delete_refuses_and_preserves_a_busy_thread(store: MessageStore) -> None:
    # The guard now lives in the manager: driving delete directly with the real
    # session lock held must leave the row and transcript untouched.
    manager = make_manager(FakeProvider([]), store)
    await store.ensure_session("cli:busy", "cli")
    await store.append("cli:busy", [{"role": "user", "content": "hi"}])
    session = await manager.get_or_create("cli:busy", "cli")
    async with session.lock:  # a turn holds this for its whole duration
        assert await manager.delete("cli:busy") is False
    assert await store.channel("cli:busy") == "cli"
    assert await store.load("cli:busy") == [{"role": "user", "content": "hi"}]


async def test_clear_refuses_and_preserves_a_busy_thread(store: MessageStore) -> None:
    manager = make_manager(FakeProvider([]), store)
    await store.ensure_session("cli:busy", "cli")
    await store.append("cli:busy", [{"role": "user", "content": "hi"}])
    session = await manager.get_or_create("cli:busy", "cli")
    async with session.lock:
        assert await manager.clear("cli:busy") is False
    assert await store.load("cli:busy") == [{"role": "user", "content": "hi"}]


async def test_delete_and_clear_wipe_an_idle_thread(store: MessageStore) -> None:
    manager = make_manager(FakeProvider([]), store)
    await store.ensure_session("cli:a", "cli")
    await store.ensure_session("cli:b", "cli")
    await store.append("cli:b", [{"role": "user", "content": "hi"}])
    # Cache both live so the busy guard's lock path (not the no-session path) runs.
    await manager.get_or_create("cli:a", "cli")
    await manager.get_or_create("cli:b", "cli")

    assert await manager.delete("cli:a") is True
    assert await store.channel("cli:a") is None

    assert await manager.clear("cli:b") is True
    assert await store.load("cli:b") == []
    assert await store.channel("cli:b") == "cli"
