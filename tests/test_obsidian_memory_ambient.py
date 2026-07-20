"""Ambient recall hook: counter cadence, owner-gating, per-candidate relevance
gate, and pointer-only injection.

Drives the real hook against a real HookRegistry and (for injection) a real
Session, with the gate and the turn on separate FakeProviders so their scripts
stay independent. The gate is a real ``Classifier`` over the definition the
package ships, so these tests break if that prompt's labels drift. The
session-scoped ``embedder`` is seeded into the module model cache so the hook's
own index construction reuses it.
"""

import asyncio
import logging
import shutil
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
from chief.classifiers import Classifier, ClassifierRegistry
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


def _classifier(judge: FakeProvider, data_dir: Path) -> Classifier:
    """A real Classifier over the definition the package actually ships, backed
    by the scripted judge provider — so these tests exercise the shipped prompt
    and label set, not a stand-in."""
    defs = data_dir / "classifiers"
    defs.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        Path("packages/obsidian-memory/classifiers/memory-relevance.md"),
        defs / "memory-relevance.md",
    )
    return Classifier(judge, ClassifierRegistry(defs), "judge-model")


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
        models={"default": "d"},
        config=config,
        data_dir=data_dir,
        logger=logging.getLogger("test.obsidian-memory"),
        budget=Budget(make_session_factory(engine), 0.0),
        classifier=_classifier(judge, data_dir),
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
    judge = FakeProvider([text_turn("RELEVANT") for _ in range(32)])
    recall = _hook(_context(judge, vault, tmp_path / "data", engine))
    fired, gated = [], []
    for turn in range(1, 13):
        before = len(judge.calls)
        if await recall(_owner("the roof leaks in the rain")) is not None:
            fired.append(turn)
        if len(judge.calls) > before:
            gated.append(turn)
    assert fired == [1, 6, 11]
    # The gate ran only on the firing turns. Asserted as growth rather than a
    # total: a firing now costs one call per candidate, not one call.
    assert gated == [1, 6, 11]


async def test_non_owner_turn_injects_nothing_and_calls_no_judge(
    vault: Path, tmp_path: Path, engine: AsyncEngine
) -> None:
    judge = FakeProvider([text_turn("RELEVANT") for _ in range(8)])
    recall = _hook(_context(judge, vault, tmp_path / "data", engine))

    system_turn = TurnContext("wake", [], "system", "cli:t", "cli")
    assert await recall(system_turn) is None
    assert judge.calls == []  # owner-gate short-circuits before any judge call

    # The gated turn also never advanced the cadence: the first owner turn fires.
    assert await recall(_owner("the roof leaks in the rain")) is not None
    assert judge.calls  # the gate ran on the owner turn


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
    judge = FakeProvider([text_turn("RELEVANT") for _ in range(8)])
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
    judge = FakeProvider([text_turn("RELEVANT") for _ in range(8)])
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


async def test_gate_sees_the_transcript_and_exactly_one_candidate_per_call(
    vault: Path, tmp_path: Path, engine: AsyncEngine
) -> None:
    """Each candidate is judged alone, transcript first.

    Transcript-first is not cosmetic: every call in a firing shares that prefix,
    so a caching provider pays for it once. The transcript now rides in the
    *user* message — the classifier definition owns ``system`` — which also
    keeps relayed conversation text out of the instruction channel.
    """
    judge = FakeProvider([text_turn("IRRELEVANT") for _ in range(8)])
    recall = _hook(_context(judge, vault, tmp_path / "data", engine))
    messages = [
        {"role": "user", "content": "we were talking about the garage"},
        {"role": "assistant", "content": "right, the garage"},
    ]
    assert await recall(_owner("the roof over there leaks", messages)) is None

    sent = judge.calls[0]
    assert [m["role"] for m in sent] == ["system", "user"]
    payload = sent[1]["content"]
    assert "we were talking about the garage" in payload
    assert "the roof over there leaks" in payload
    # Singular: one note per call, and the transcript leads it.
    assert payload.count("Candidate note:") == 1
    assert payload.index("Conversation so far:") < payload.index("Candidate note:")


async def test_one_gate_call_per_candidate(
    vault: Path, tmp_path: Path, engine: AsyncEngine
) -> None:
    judge = FakeProvider([text_turn("IRRELEVANT") for _ in range(8)])
    context = _context(judge, vault, tmp_path / "data", engine)
    recall = _hook(context)
    assert await recall(_owner("the roof leaks in the rain")) is None
    # Every fetched candidate got its own yes/no call — no all-at-once judge.
    assert len(judge.calls) >= 1
    assert all(len(call) == 2 for call in judge.calls)


async def test_a_classifier_that_never_resolves_drops_the_candidate(
    vault: Path, tmp_path: Path, engine: AsyncEngine
) -> None:
    """A gate that can't produce a label must not fail the turn.

    The old judge's malformed-output path silently injected nothing, which was
    indistinguishable from "nothing was relevant". Here the candidate is simply
    dropped — recall degrades, the turn survives.
    """
    # Never a valid label: the classifier exhausts its 3 tries per candidate.
    judge = FakeProvider([text_turn("maybe?") for _ in range(64)])
    recall = _hook(_context(judge, vault, tmp_path / "data", engine))
    assert await recall(_owner("the roof leaks in the rain")) is None


async def test_accepted_hits_land_in_the_hook_block_as_pointers_not_content(
    vault: Path, tmp_path: Path, engine: AsyncEngine, store: MessageStore
) -> None:
    """Recall nudges, it does not inject.

    The block names the note and its heading; the body stays in the vault for
    the agent to open with its file tools if it decides the note is worth it.
    """
    judge = FakeProvider([text_turn("RELEVANT") for _ in range(8)])
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
    # The pointer, not the payload: no sentence from the note's body.
    assert "shingles above the north corner" not in system


async def test_run_judge_truncates_to_the_token_cap(tmp_path: Path) -> None:
    from chief_obsidian_memory.index import SearchHit
    from chief_obsidian_memory.judge import run_judge

    judge = FakeProvider([text_turn("RELEVANT")])
    huge = SearchHit("big.md", "H" * 5000, "body", 0.9)
    out = await run_judge(
        _classifier(judge, tmp_path), "transcript", [huge], cap_tokens=10
    )
    assert out is not None
    assert len(out) <= 10 * 4 + 1  # cap in chars, plus the ellipsis


async def test_irrelevant_candidates_are_dropped(tmp_path: Path) -> None:
    from chief_obsidian_memory.index import SearchHit
    from chief_obsidian_memory.judge import run_judge

    judge = FakeProvider([text_turn("IRRELEVANT"), text_turn("RELEVANT")])
    hits = [
        SearchHit("noise.md", "Unrelated", "body", 0.9),
        SearchHit("roof.md", "Roof repair", "body", 0.8),
    ]
    out = await run_judge(
        _classifier(judge, tmp_path), "transcript", hits, cap_tokens=1500
    )
    assert out is not None
    assert "roof.md" in out
    assert "noise.md" not in out


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
