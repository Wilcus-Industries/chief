"""The obsidian-memory agent-loop hooks: ambient recall + a standing reminder.

``register`` is the package entry point the boot loader calls. It stays light —
config only — and defers every heavy import (the index, the judge, and through
them chromadb/model2vec) to the moment the recall hook actually fires, so boot
never pays for the vector stack. Recall is owner-gated first of all: a non-owner
turn (a monitor/cron ``system`` wake, a stranger) never reaches the vault.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from chief_obsidian_memory.config import MemorySettings

if TYPE_CHECKING:
    from chief.hooks import HookContext, PackageHookRegistrar, TurnContext

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
    index_home = context.data_dir / "index"
    counters: dict[str, int] = {}

    @hooks.pre_turn
    async def recall(turn: TurnContext) -> str | None:
        # Owner-gate first: private vault data never enters a non-owner turn,
        # and a non-owner turn never advances the owner's recall cadence.
        if turn.sender != "owner":
            return None
        counters[turn.thread_key] = counters.get(turn.thread_key, 0) + 1
        if (counters[turn.thread_key] - 1) % settings.ambient_n != 0:
            return None
        vault = _vault(settings)
        if vault is None or not model:
            return None
        return await _recall(context, model, settings, index_home, vault, turn)

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
) -> str | None:
    # Heavy imports live here: importing this module at boot must not pull in
    # chromadb/model2vec (asserted by the test suite).
    from chief_obsidian_memory.index import VaultIndex
    from chief_obsidian_memory.judge import format_transcript, run_judge

    index = VaultIndex(vault, index_home, settings)
    candidates = index.search(turn.user_text, settings.top_k)
    transcript = format_transcript(turn.messages, turn.user_text, settings.window)
    return await run_judge(
        context.provider, model, transcript, candidates,
        settings.injection_cap_tokens,
    )


def _vault(settings: MemorySettings) -> Path | None:
    """The first configured vault path that exists, or None (recall no-ops)."""
    for candidate in settings.vault_paths:
        path = Path(candidate)
        if path.is_dir():
            return path
    return None
