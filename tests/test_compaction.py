"""Compaction: folding old history into a note, live and persisted."""

import asyncio
from typing import Any

from chief.agent.compaction import NOTE_PREFIX, Compactor, estimate_tokens
from chief.agent.session import Session
from chief.persistence.store import MessageStore
from chief.tools import ToolRegistry

from .fakes import FakeProvider, text_turn


class FixedWindow:
    """A WindowSource that reports one window for every model (test double)."""

    def __init__(self, window: int) -> None:
        self.window = window

    async def resolve(self, model: str) -> int:
        return self.window


def compactor_for(
    provider: FakeProvider,
    messages: list[dict[str, Any]],
    *,
    keep_recent: int,
) -> Compactor:
    """A Compactor whose threshold lands just under ``messages`` (ratio=1.0, so
    the window is the threshold) — the old ``threshold=`` construction, now
    expressed through the per-model window seam."""
    return Compactor(
        provider,
        "m",
        FixedWindow(estimate_tokens(messages) - 1),
        ratio=1.0,
        keep_recent=keep_recent,
    )


def chat(n: int) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for i in range(n):
        turns.append({"role": "user", "content": f"question {i} " + "x" * 200})
        turns.append({"role": "assistant", "content": f"answer {i} " + "y" * 200})
    return turns


async def test_under_threshold_is_untouched() -> None:
    provider = FakeProvider([])
    compactor = Compactor(provider, "m", FixedWindow(10_000), ratio=1.0, keep_recent=4)
    assert await compactor.compact(chat(3)) is None
    assert provider.calls == []


async def test_bigger_window_avoids_compaction() -> None:
    # Same transcript, but a large window keeps it under threshold: no compact.
    provider = FakeProvider([text_turn("s")])
    messages = chat(20)
    compactor = Compactor(
        provider, "m", FixedWindow(10_000_000), ratio=0.95, keep_recent=4
    )
    assert await compactor.compact(messages) is None
    assert provider.calls == []


async def test_ratio_lowers_the_threshold() -> None:
    # A big window but a tiny ratio pulls the threshold below the transcript.
    provider = FakeProvider([text_turn("s")])
    messages = chat(20)
    window = estimate_tokens(messages) * 10
    compactor = Compactor(provider, "m", FixedWindow(window), ratio=0.01, keep_recent=4)
    assert await compactor.compact(messages) is not None


async def test_force_compacts_below_threshold() -> None:
    provider = FakeProvider([text_turn("forced summary")])
    messages = chat(20)
    # Window is enormous, so the threshold would never trip on its own.
    compactor = Compactor(
        provider, "m", FixedWindow(10_000_000), ratio=0.95, keep_recent=4
    )
    compacted = await compactor.compact(messages, force=True)
    assert compacted is not None
    assert compacted[0]["content"] == NOTE_PREFIX + "forced summary"


async def test_empty_summary_aborts_instead_of_destroying_history() -> None:
    # A model refusal / content-filter yields an empty summary. Truncating to an
    # empty note would permanently discard the history for nothing — abort.
    provider = FakeProvider([text_turn("")])
    messages = chat(20)
    compactor = Compactor(
        provider, "m", FixedWindow(10_000_000), ratio=0.95, keep_recent=4
    )
    assert await compactor.compact(messages, force=True) is None


async def test_compacts_old_history_into_a_leading_note() -> None:
    provider = FakeProvider([text_turn("the dense summary")])
    messages = chat(20)
    compactor = compactor_for(provider, messages, keep_recent=4)
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
    compactor = compactor_for(provider, messages, keep_recent=3)
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
        compactor=compactor_for(provider, history, keep_recent=4),
    )
    result = await session.run_turn("new question", lambda _: asyncio.sleep(0))
    assert result.text == "the reply"
    persisted = await store.load("t")
    assert persisted[0]["content"].startswith(NOTE_PREFIX + "summary note")
    assert [m["role"] for m in persisted[-2:]] == ["user", "assistant"]
    assert persisted[-2]["content"] == "new question"
    assert len(persisted) == 1 + 4 + 2  # note + kept tail + the new turn


async def test_session_force_compact_below_threshold(store: MessageStore) -> None:
    history = chat(20)
    await store.ensure_session("t", "cli")
    await store.append("t", history)
    provider = FakeProvider([text_turn("forced note")])
    session = Session(
        thread_key="t",
        provider=provider,
        tools=ToolRegistry(),
        store=store,
        model="m",
        system_prompt="s",
        history=list(history),
        turn_semaphore=asyncio.Semaphore(1),
        # Huge window: the threshold never trips, so only force() compacts.
        compactor=Compactor(
            provider, "m", FixedWindow(10_000_000), ratio=0.95, keep_recent=4
        ),
    )
    reply = await session.compact()
    assert reply.startswith("compacted")
    persisted = await store.load("t")
    assert persisted[0]["content"].startswith(NOTE_PREFIX + "forced note")
    assert len(persisted) == 1 + 4  # note + kept tail, no new turn


async def test_session_force_compact_reports_nothing_to_compact(
    store: MessageStore,
) -> None:
    await store.ensure_session("t", "cli")
    provider = FakeProvider([])
    session = Session(
        thread_key="t",
        provider=provider,
        tools=ToolRegistry(),
        store=store,
        model="m",
        system_prompt="s",
        history=[],
        turn_semaphore=asyncio.Semaphore(1),
        compactor=Compactor(
            provider, "m", FixedWindow(10_000_000), ratio=0.95, keep_recent=4
        ),
    )
    assert await session.compact() == "nothing to compact"
    assert provider.calls == []
