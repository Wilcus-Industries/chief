"""Compaction: folding old history into a note, live and persisted."""

import asyncio
from typing import Any

from chief.agent.compaction import NOTE_PREFIX, Compactor, estimate_tokens
from chief.agent.session import Session
from chief.agent.tools import ToolRegistry
from chief.persistence.store import MessageStore

from .fakes import FakeProvider, text_turn


def chat(n: int) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for i in range(n):
        turns.append({"role": "user", "content": f"question {i} " + "x" * 200})
        turns.append({"role": "assistant", "content": f"answer {i} " + "y" * 200})
    return turns


async def test_under_threshold_is_untouched() -> None:
    provider = FakeProvider([])
    compactor = Compactor(provider, "m", threshold=10_000, keep_recent=4)
    assert await compactor.compact(chat(3)) is None
    assert provider.calls == []


async def test_compacts_old_history_into_a_leading_note() -> None:
    provider = FakeProvider([text_turn("the dense summary")])
    messages = chat(20)
    compactor = Compactor(
        provider, "m", threshold=estimate_tokens(messages) - 1, keep_recent=4
    )
    compacted = await compactor.compact(messages)
    assert compacted is not None
    assert compacted[0]["role"] == "system"
    assert compacted[0]["content"] == NOTE_PREFIX + "the dense summary"
    tail = compacted[1:]
    assert tail[0]["role"] == "user"  # split lands on a turn boundary
    assert tail == messages[-4:]
    # The summarize call saw the old half, not the kept tail.
    assert "question 0" in provider.calls[0][1]["content"]
    assert "question 19" not in provider.calls[0][1]["content"]


async def test_split_never_severs_a_tool_exchange() -> None:
    messages = chat(6)
    messages.extend(
        [
            {"role": "user", "content": "do a thing " + "z" * 200},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c"}]},
            {"role": "tool", "tool_call_id": "c", "content": "result " + "z" * 200},
            {"role": "assistant", "content": "done " + "z" * 200},
        ]
    )
    provider = FakeProvider([text_turn("s")])
    compactor = Compactor(
        provider, "m", threshold=estimate_tokens(messages) - 1, keep_recent=3
    )
    compacted = await compactor.compact(messages)
    assert compacted is not None
    # keep_recent=3 would cut inside the tool exchange; the whole exchange
    # gets summarized instead, and nothing dangles.
    assert all(m.get("role") != "tool" for m in compacted[:1])
    assert compacted[1:] == [] or compacted[1]["role"] == "user"


async def test_session_compacts_live_and_persisted_history(
    store: MessageStore,
) -> None:
    history = chat(20)
    await store.ensure_session("t", "cli")
    await store.append("t", history)
    provider = FakeProvider([text_turn("summary note"), text_turn("the reply")])
    session = Session(
        thread_key="t",
        provider=provider,
        tools=ToolRegistry(),
        store=store,
        model="m",
        system_prompt="s",
        history=list(history),
        turn_semaphore=asyncio.Semaphore(1),
        compactor=Compactor(
            provider, "m", threshold=estimate_tokens(history) - 1, keep_recent=4
        ),
    )
    result = await session.run_turn("new question", lambda _: asyncio.sleep(0))
    assert result.text == "the reply"
    persisted = await store.load("t")
    assert persisted[0]["content"].startswith(NOTE_PREFIX + "summary note")
    assert [m["role"] for m in persisted[-2:]] == ["user", "assistant"]
    assert persisted[-2]["content"] == "new question"
    assert len(persisted) == 1 + 4 + 2  # note + kept tail + the new turn
