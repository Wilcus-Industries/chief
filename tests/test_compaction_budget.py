"""The message budget: threads that reach more than the owner compact early."""

import asyncio
from pathlib import Path

from chief.agent.compaction import NOTE_PREFIX, Compactor, message_tokens
from chief.agent.session import Session
from chief.config import load_config
from chief.persistence.store import MessageStore
from chief.tools import ToolRegistry

from .fakes import FakeProvider, text_turn
from .test_compaction import FixedWindow, chat

HUGE = FixedWindow(10_000_000)  # the window alone would never compact


def shared(thread: str, channel: str) -> bool:
    return thread == "chat123"


def budgeted(provider: FakeProvider, budget: int) -> Compactor:
    return Compactor(provider, "m", HUGE, ratio=0.95, keep_recent=4,
                     message_budget=budget, budgeted=shared)


async def test_a_shared_thread_compacts_at_its_message_budget() -> None:
    provider = FakeProvider([text_turn("gc summary")])
    messages = chat(20)
    compactor = budgeted(provider, message_tokens(messages) - 1)
    compacted = await compactor.compact(messages, thread=("chat123", "imessage"))
    assert compacted is not None
    assert compacted[0]["content"] == NOTE_PREFIX + "gc summary"
    assert compacted[1:] == messages[-4:]


async def test_an_owner_thread_ignores_the_budget() -> None:
    provider = FakeProvider([])
    messages = chat(20)
    compactor = budgeted(provider, 1)
    assert await compactor.compact(messages, thread=("+1555", "imessage")) is None
    assert await compactor.compact(messages) is None, "no thread, no budget"
    assert provider.calls == []


async def test_the_summary_note_does_not_count_toward_the_budget() -> None:
    provider = FakeProvider([])
    tail = chat(4)
    note = {"role": "system", "content": NOTE_PREFIX + "z" * 40_000}
    compactor = budgeted(provider, message_tokens(tail) + 1)
    assert message_tokens([note, *tail]) == message_tokens(tail)
    assert await compactor.compact([note, *tail], thread=("chat123", "x")) is None


async def test_zero_budget_turns_it_off() -> None:
    provider = FakeProvider([])
    compactor = budgeted(provider, 0)
    assert await compactor.compact(chat(20), thread=("chat123", "x")) is None


async def test_session_applies_the_budget_to_its_own_thread(
    store: MessageStore,
) -> None:
    history = chat(20)
    await store.ensure_session("chat123", "imessage")
    await store.append("chat123", history)
    provider = FakeProvider([text_turn("note"), text_turn("reply")])
    session = Session(
        thread_key="chat123", provider=provider, tools=ToolRegistry(),
        store=store, model="m", system_prompt="s", history=list(history),
        turn_semaphore=asyncio.Semaphore(1), origin_channel="imessage",
        compactor=budgeted(provider, message_tokens(history) - 1),
    )
    await session.run_turn("new", lambda _: asyncio.sleep(0))
    persisted = await store.load("chat123")
    assert persisted[0]["content"].startswith(NOTE_PREFIX + "note")


def test_budget_config_defaults_and_override(tmp_path: Path) -> None:
    assert load_config(tmp_path / "none.yaml").compaction_message_budget == 90_000
    path = tmp_path / "config.yaml"
    path.write_text("compaction:\n  message_budget: 50000\n")
    assert load_config(path).compaction_message_budget == 50_000
