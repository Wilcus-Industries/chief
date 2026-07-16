"""Sessions: serial turn queue, persistence, restart resume, concurrency cap."""

import asyncio

from chief.agent.manager import SessionManager
from chief.agent.tools import ToolRegistry
from chief.persistence.store import MessageStore

from .fakes import FakeProvider, text_turn


async def noop_delta(text: str) -> None:
    pass


def make_manager(
    provider: FakeProvider, store: MessageStore, max_concurrent: int = 4
) -> SessionManager:
    return SessionManager(
        provider=provider,
        registry=ToolRegistry(),
        store=store,
        default_model="test-model",
        system_prompt="test system prompt",
        max_concurrent=max_concurrent,
    )


async def test_turn_persists_transcript_and_injects_system_prompt(
    store: MessageStore,
) -> None:
    provider = FakeProvider([text_turn("hi there")])
    manager = make_manager(provider, store)
    session = await manager.get_or_create("cli:t", "cli")
    result = await session.run_turn("hello", noop_delta)
    assert result.text == "hi there"
    assert provider.calls[0][0] == {"role": "system", "content": "test system prompt"}
    saved = await store.load("cli:t")
    assert saved == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


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


async def test_get_or_create_returns_the_same_session(store: MessageStore) -> None:
    provider = FakeProvider([])
    manager = make_manager(provider, store)
    a = await manager.get_or_create("cli:t", "cli")
    b = await manager.get_or_create("cli:t", "cli")
    assert a is b
