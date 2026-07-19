"""Ambient recall hook: counter cadence, owner-gating, one-message judge, cap.

Drives the real hook against a real HookRegistry and (for injection) a real
Session, with the judge and the turn on separate FakeProviders so their scripts
stay independent. The session-scoped ``embedder`` is seeded into the module
model cache so the hook's own index construction reuses it.
"""

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from chief_obsidian_memory import embedding
from chief_obsidian_memory.hook import register
from sqlalchemy.ext.asyncio import AsyncEngine

from chief.agent.manager import SessionManager
from chief.agent.tools import ToolRegistry
from chief.budget import Budget
from chief.hooks import (
    HookContext,
    HookRegistry,
    PackageHookRegistrar,
    TurnContext,
)
from chief.persistence.db import make_session_factory
from chief.persistence.store import MessageStore

from .fakes import FakeProvider, text_turn


@pytest.fixture(autouse=True)
def _warm_model(embedder: Any) -> Any:
    embedding._MODEL_CACHE["minishlab/potion-base-8M"] = embedder
    yield
    embedding._MODEL_CACHE.clear()


async def noop_delta(text: str) -> None:
    pass


def _context(
    judge: FakeProvider, vault: Path, data_dir: Path, engine: AsyncEngine, **cfg: Any
) -> HookContext:
    config = {
        "obsidian_memory": {
            "vault_paths": [str(vault)],
            "ambient_n": 5,
            "top_k": 4,
            **cfg,
        }
    }
    return HookContext(
        provider=judge,
        models={"memory_judge": "judge-model", "default": "d"},
        config=config,
        data_dir=data_dir,
        logger=logging.getLogger("test.obsidian-memory"),
        budget=Budget(make_session_factory(engine), 0.0),
    )


def _hook(context: HookContext) -> Any:
    registry = HookRegistry()
    register(context, PackageHookRegistrar(registry, "obsidian-memory"))
    return registry.pre_turn()[0][1]


def _owner(text: str, messages: list[dict[str, Any]] | None = None) -> TurnContext:
    return TurnContext(text, messages or [], "owner", "cli:t", "cli")


async def test_counter_fires_on_1_6_11_and_is_silent_between(
    vault: Path, tmp_path: Path, engine: AsyncEngine
) -> None:
    judge = FakeProvider([text_turn("1")] * 3)
    recall = _hook(_context(judge, vault, tmp_path / "data", engine))
    fired = [
        turn
        for turn in range(1, 13)
        if await recall(_owner("the roof leaks in the rain")) is not None
    ]
    assert fired == [1, 6, 11]
    assert len(judge.calls) == 3  # the judge ran only on the firing turns


async def test_non_owner_turn_injects_nothing_and_calls_no_judge(
    vault: Path, tmp_path: Path, engine: AsyncEngine
) -> None:
    judge = FakeProvider([text_turn("1")])
    recall = _hook(_context(judge, vault, tmp_path / "data", engine))

    system_turn = TurnContext("wake", [], "system", "cli:t", "cli")
    assert await recall(system_turn) is None
    assert judge.calls == []  # owner-gate short-circuits before any judge call

    # The gated turn also never advanced the cadence: the first owner turn fires.
    assert await recall(_owner("the roof leaks in the rain")) is not None
    assert len(judge.calls) == 1


def _to_thread_spy(
    monkeypatch: pytest.MonkeyPatch, calls: list[Any]
) -> None:
    """Wrap ``asyncio.to_thread`` so a test can see what gets offloaded while
    the real work still runs."""
    real = asyncio.to_thread

    async def spy(fn: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(fn)
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)


async def test_firing_recall_offloads_index_work_off_the_event_loop(
    vault: Path,
    tmp_path: Path,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The blocking index/search path (chroma open, a possible full vault build,
    # query embedding) must run via asyncio.to_thread, so one recall firing
    # can't stall the daemon's event loop — every other channel and monitor.
    judge = FakeProvider([text_turn("1")])
    recall = _hook(_context(judge, vault, tmp_path / "data", engine))
    offloaded: list[Any] = []
    _to_thread_spy(monkeypatch, offloaded)

    out = await recall(_owner("the roof leaks in the rain"))

    assert out is not None  # the judge still selected the roof candidate
    assert offloaded, "index/search work must be offloaded to a worker thread"


async def test_owner_gate_and_cadence_precede_any_thread_offload(
    vault: Path,
    tmp_path: Path,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-owner turn and a silent (non-firing) owner turn never reach the
    # thread: the fast owner-gate + cadence check stay on the loop, first.
    judge = FakeProvider([text_turn("1")])
    recall = _hook(_context(judge, vault, tmp_path / "data", engine))
    offloaded: list[Any] = []
    _to_thread_spy(monkeypatch, offloaded)

    system_turn = TurnContext("wake", [], "system", "cli:t", "cli")
    assert await recall(system_turn) is None
    assert offloaded == []  # owner-gate short-circuits before the thread

    assert await recall(_owner("first owner turn fires")) is not None
    assert len(offloaded) == 1  # turn 1 fires -> exactly one offload
    assert await recall(_owner("second owner turn is silent")) is None
    assert len(offloaded) == 1  # turn 2 is silent -> no further offload


async def test_judge_receives_the_transcript_as_one_system_message(
    vault: Path, tmp_path: Path, engine: AsyncEngine
) -> None:
    judge = FakeProvider([text_turn("none")])
    recall = _hook(_context(judge, vault, tmp_path / "data", engine))
    messages = [
        {"role": "user", "content": "we were talking about the garage"},
        {"role": "assistant", "content": "right, the garage"},
    ]
    assert await recall(_owner("the roof over there leaks", messages)) is None

    sent = judge.calls[0]
    assert len(sent) == 1
    assert sent[0]["role"] == "system"
    content = sent[0]["content"]
    assert "we were talking about the garage" in content
    assert "the roof over there leaks" in content
    assert "Candidate notes:" in content


async def test_accepted_hits_land_in_the_hook_block_within_cap(
    vault: Path, tmp_path: Path, engine: AsyncEngine, store: MessageStore
) -> None:
    judge = FakeProvider([text_turn("1")])
    registry = HookRegistry()
    register(
        _context(judge, vault, tmp_path / "data", engine),
        PackageHookRegistrar(registry, "obsidian-memory"),
    )
    session_provider = FakeProvider([text_turn("assistant reply")])
    manager = SessionManager(
        provider=session_provider,
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="m",
        system_prompt="BASE",
        max_concurrent=4,
        soul_reader=lambda: "",
        hooks=registry,
        hooks_timeout_seconds=15.0,
    )
    session = await manager.get_or_create("cli:t", "cli")
    await session.run_turn("the roof leaks in the rain", noop_delta, sender="owner")

    system = session_provider.calls[0][0]["content"]
    assert '<hook source="obsidian-memory">' in system
    assert "roof.md" in system


async def test_run_judge_truncates_to_the_token_cap() -> None:
    from chief_obsidian_memory.index import SearchHit
    from chief_obsidian_memory.judge import run_judge

    judge = FakeProvider([text_turn("1")])
    huge = SearchHit("big.md", "Heading", "x" * 5000, 0.9)
    out = await run_judge(judge, "m", "transcript", [huge], cap_tokens=10)
    assert out is not None
    assert len(out) <= 10 * 4 + 1  # cap in chars, plus the ellipsis


def test_importing_hook_module_does_not_import_the_vector_stack() -> None:
    code = (
        "import sys, chief_obsidian_memory.hook\n"
        "leaked = [m for m in ('chromadb', 'model2vec') if m in sys.modules]\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
