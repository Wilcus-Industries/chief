"""Freshness: refresh re-embeds changes, search self-heals, missing vault loud."""

import os
import time
from pathlib import Path
from typing import Any

import pytest
from chief_obsidian_memory.config import MemorySettings
from chief_obsidian_memory.index import VaultIndex


def _index(vault: Path, embedder: Any) -> VaultIndex:
    return VaultIndex(
        vault=vault,
        index_home=vault.parent / "index",
        settings=MemorySettings(),
        model=embedder,
    )


def test_refresh_reembeds_an_out_of_band_edit(vault: Path, embedder: Any) -> None:
    index = _index(vault, embedder)
    index.build()
    (vault / "garden.md").write_text(
        "# Composting\n\nTurn the heap weekly so scraps rot down into crumbly "
        "dark soil.\n"
    )
    assert index.refresh() >= 1
    hits = index.search("rotting kitchen scraps breaking down into soil", k=3)
    assert hits[0].note_path == "garden.md"
    assert hits[0].heading == "Composting"


def test_refresh_drops_a_deleted_note(vault: Path, embedder: Any) -> None:
    index = _index(vault, embedder)
    index.build()
    (vault / "garden.md").unlink()
    assert index.refresh() >= 1
    hits = index.search("summer vegetable beds with basil and peppers", k=5)
    assert all(h.note_path != "garden.md" for h in hits)


def test_search_reembeds_a_note_that_drifted_out_of_band(
    vault: Path, embedder: Any
) -> None:
    index = _index(vault, embedder)
    index.build()
    note = vault / "tomatoes.md"
    note.write_text("# Blight\n\nEarly blight fungus spots the lower leaves.\n")
    future = time.time() + 100
    os.utime(note, (future, future))
    # No explicit refresh: the query-time staleness check must notice the newer
    # mtime on a ranked note and re-embed it before returning.
    hits = index.search("problems affecting the tomato plant leaves", k=5)
    assert any(h.note_path == "tomatoes.md" and h.heading == "Blight" for h in hits)


def test_dropped_collection_auto_rebuilds_on_search(
    vault: Path, embedder: Any
) -> None:
    index = _index(vault, embedder)
    index.build()
    # A missing/corrupt collection at open must trigger a full rebuild, never a
    # silently empty index.
    index._client_().delete_collection(index._collection_name())
    index._collection = None
    hits = index.search("water leaking through the ceiling", k=3)
    assert hits and hits[0].note_path == "roof.md"


def test_missing_vault_fails_loudly(tmp_path: Path, embedder: Any) -> None:
    index = VaultIndex(
        vault=tmp_path / "ghost",
        index_home=tmp_path / "index",
        settings=MemorySettings(),
        model=embedder,
    )
    with pytest.raises(FileNotFoundError):
        index.build()
    # Search self-heals via build, so a missing vault surfaces there too.
    with pytest.raises(FileNotFoundError):
        index.search("anything", k=3)
