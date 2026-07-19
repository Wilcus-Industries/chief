"""The obsidian-memory agent-loop hooks: ambient recall + a standing reminder.

``register`` is the package entry point the boot loader calls. It stays light —
config only — and defers every heavy import (the index, the judge, and through
them chromadb/model2vec) to the moment the recall hook actually fires, so boot
never pays for the vector stack. Recall is owner-gated first of all: a non-owner
turn (a monitor/cron ``system`` wake, a stranger) never reaches the vault.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from chief_obsidian_memory.config import MemorySettings, index_home_for

if TYPE_CHECKING:
    from chief.hooks import HookContext, PackageHookRegistrar, TurnContext
    from chief_obsidian_memory.index import SearchHit

STANDING_REMINDER = (
    "You keep an Obsidian memory vault. Recall from it with the obsidian-memory "
    "skill and the `chief-memory` CLI, and save durable facts the owner shares "
    "as vault notes when your writable paths allow it."
)


def register(context: HookContext, hooks: PackageHookRegistrar) -> None:
    """Wire the ambient recall pre_turn hook and the session-start reminder."""
    settings = MemorySettings.from_config(context.config.get("obsidian_memory"))
    model = context.models.get(settings.judge_role) or context.models.get(
        "default", ""
    )
    index_home = index_home_for(context.data_dir)
    counters: dict[str, int] = {}
    # Guards the chroma index build/rebuild once two thread firings genuinely
    # run in parallel; unrelated (non-recall) turns never touch it.
    build_lock = threading.Lock()

    @hooks.pre_turn
    async def recall(turn: TurnContext) -> str | None:
        # Owner-gate + cadence stay on the event loop, first and cheap: private
        # vault data never enters a non-owner turn, a non-owner turn never
        # advances the owner's cadence, and neither ever reaches the thread.
        if turn.sender != "owner":
            return None
        counters[turn.thread_key] = counters.get(turn.thread_key, 0) + 1
        if (counters[turn.thread_key] - 1) % settings.ambient_n != 0:
            return None
        vault = _vault(settings)
        if vault is None or not model:
            return None
        return await _recall(
            context, model, settings, index_home, vault, turn, build_lock
        )

    @hooks.session_start
    async def reminder(_turn: TurnContext) -> str | None:
        # No vault data, so no owner-gate needed — just a standing capability
        # note the agent sees once per thread.
        return STANDING_REMINDER


async def _recall(
    context: HookContext,
    model: str,
    settings: MemorySettings,
    index_home: Path,
    vault: Path,
    turn: TurnContext,
    build_lock: threading.Lock,
) -> str | None:
    from chief_obsidian_memory.judge import format_transcript, run_judge

    # The heavy, blocking work — opening chroma, a possible full vault build
    # (embedding every chunk, worst case a model2vec weight download), and the
    # in-process candidate fetch — runs off the event loop. asyncio.wait_for
    # (#208's per-hook timeout) still can't cancel an in-flight thread, but the
    # loop — every other turn, channel, and monitor — is no longer blocked;
    # that is the property this restores. The async judge call stays awaited.
    candidates = await asyncio.to_thread(
        _fetch_candidates, vault, index_home, settings, turn.user_text, build_lock
    )
    transcript = format_transcript(turn.messages, turn.user_text, settings.window)
    return await run_judge(
        context.provider, model, transcript, candidates,
        settings.injection_cap_tokens,
    )


def _fetch_candidates(
    vault: Path,
    index_home: Path,
    settings: MemorySettings,
    query: str,
    build_lock: threading.Lock,
) -> list[SearchHit]:
    # Heavy imports live here: importing this module at boot must not pull in
    # chromadb/model2vec (asserted by the test suite).
    from chief_obsidian_memory.index import VaultIndex

    index = VaultIndex(vault, index_home, settings)
    # search() may build/self-heal the collection; serialize so two parallel
    # firings can't race a concurrent create/rebuild of the same chroma store.
    with build_lock:
        return index.search(query, settings.top_k)


def _vault(settings: MemorySettings) -> Path | None:
    """The first configured vault path that exists, or None (recall no-ops)."""
    for candidate in settings.vault_paths:
        path = Path(candidate)
        if path.is_dir():
            return path
    return None
