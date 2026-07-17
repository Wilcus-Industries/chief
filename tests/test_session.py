"""Sessions: serial turn queue, persistence, restart resume, concurrency cap."""

import asyncio
from typing import Any

from chief.agent.manager import SessionManager
from chief.agent.tools import Tool, ToolRegistry
from chief.persistence.store import MessageStore
from chief.provider.base import ToolSpec
from chief.selfedit.recovery import RestartController

from .fakes import FakeProvider, text_turn, tool_turn


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

    async def fake_self_edit() -> str:
        controller.request()
        return "self-edit applied; restarting"

    registry.register(
        Tool(ToolSpec(name="self_edit", description="", parameters={}), fake_self_edit)
    )

    original_append = store.append

    async def traced_append(thread_key: str, messages: list[dict[str, Any]]) -> None:
        events.append("commit")
        await original_append(thread_key, messages)

    store.append = traced_append  # type: ignore[method-assign]

    provider = FakeProvider([tool_turn("self_edit", {}), text_turn("done, back soon")])
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

    # Committed, but the restart is only requested — not fired here.
    assert events == ["commit"]
    saved = await store.load("cli:t")
    assert saved[0] == {"role": "user", "content": "install imessage"}
    assert saved[-1] == {"role": "assistant", "content": "done, back soon"}
    # The pending restart fires once the boundary owner calls it.
    await controller.fire_if_requested()
    assert events == ["commit", "restart"]


async def test_get_or_create_returns_the_same_session(store: MessageStore) -> None:
    provider = FakeProvider([])
    manager = make_manager(provider, store)
    a = await manager.get_or_create("cli:t", "cli")
    b = await manager.get_or_create("cli:t", "cli")
    assert a is b
