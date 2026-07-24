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
    note = vault / "garden.md"
    note.write_text(
        "# Composting\n\nTurn the heap weekly so scraps rot down into crumbly "
        "dark soil.\n"
    )
    # The sweep is mtime-gated: bump the mtime so the edit is a candidate
    # regardless of filesystem timestamp granularity (an edit that does not
    # advance mtime is the documented accepted corner, caught only by reindex).
    future = time.time() + 100
    os.utime(note, (future, future))
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
    # No explicit refresh: the pre-query sweep must notice the newer mtime and
    # re-embed the note before the query runs.
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


class _CountingModel:
    """Wraps a real embedder and counts ``encode`` calls, so a test can assert a
    search re-embeds nothing when the vault is untouched."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def encode(self, texts: list[str]) -> Any:
        self.calls += 1
        return self._inner.encode(texts)


def test_search_indexes_a_new_note_without_a_reindex(
    vault: Path, embedder: Any
) -> None:
    # The headline: a note that appears after the last build is picked up by the
    # next search itself — no manual reindex, and search() never calls build().
    index = _index(vault, embedder)
    index.build()
    (vault / "shed.md").write_text(
        "# Lawn mower\n\nThe orange push mower lives behind the blue kayak.\n"
    )
    hits = index.search("where is the push mower kept in the shed", k=5)
    assert any(h.note_path == "shed.md" for h in hits)


def test_search_drops_a_deleted_note_without_a_reindex(
    vault: Path, embedder: Any
) -> None:
    index = _index(vault, embedder)
    index.build()
    (vault / "garden.md").unlink()
    hits = index.search("summer vegetable beds with basil and peppers", k=5)
    assert all(h.note_path != "garden.md" for h in hits)


def test_search_over_an_untouched_vault_reembeds_nothing(
    vault: Path, embedder: Any
) -> None:
    # "Only re-embeds changed": with nothing changed, the sole encode call is the
    # query itself — the sweep stats the notes and reads none.
    model = _CountingModel(embedder)
    index = _index(vault, model)
    index.build()
    model.calls = 0
    index.search("leaking roof in the rain", k=3)
    assert model.calls == 1


def test_search_survives_an_unreadable_note_added_to_the_vault(
    vault: Path, embedder: Any
) -> None:
    # One note that won't decode (non-UTF-8) must not abort the whole sweep and
    # take recall down with it — search still returns the other notes' hits.
    index = _index(vault, embedder)
    index.build()
    (vault / "corrupt.md").write_bytes(b"# Junk\n\n\xff\xfe not utf-8\n")
    hits = index.search("water leaking through the ceiling", k=3)
    assert hits and hits[0].note_path == "roof.md"


def test_auto_refresh_false_skips_the_in_search_sweep(
    vault: Path, embedder: Any
) -> None:
    # The escape valve: with auto_refresh off, search does not sweep, so a new
    # note stays invisible until a manual refresh/reindex.
    settings = MemorySettings(auto_refresh=False)
    index = VaultIndex(
        vault=vault, index_home=vault.parent / "idx", settings=settings, model=embedder
    )
    index.build()
    (vault / "shed.md").write_text("# Kayak\n\nThe blue kayak hangs on the wall.\n")
    hits = index.search("where does the kayak hang", k=5)
    assert all(h.note_path != "shed.md" for h in hits)
    # A manual refresh still reconciles it — the gate is on the automatic path.
    assert index.refresh() >= 1
    hits = index.search("where does the kayak hang", k=5)
    assert any(h.note_path == "shed.md" for h in hits)


def test_refresh_min_interval_throttles_the_in_search_sweep(
    vault: Path, embedder: Any
) -> None:
    # The throttle skips the sweep if one ran within the window, using an
    # injected clock and the on-disk per-vault stamp.
    clock = {"t": 1000.0}
    settings = MemorySettings(refresh_min_interval_s=100)
    index = VaultIndex(
        vault=vault,
        index_home=vault.parent / "idx",
        settings=settings,
        model=embedder,
        now=lambda: clock["t"],
    )
    index.build()
    # First search sweeps (no-op) and stamps the clock at t=1000.
    index.search("leaking roof in the rain", k=3)
    (vault / "shed.md").write_text("# Ladder\n\nThe aluminium ladder is in the shed.\n")
    # Within the window: throttled, so the new note is not visible yet.
    clock["t"] = 1050.0
    hits = index.search("where is the aluminium ladder", k=5)
    assert all(h.note_path != "shed.md" for h in hits)
    # Past the window: the sweep runs and the note appears.
    clock["t"] = 1200.0
    hits = index.search("where is the aluminium ladder", k=5)
    assert any(h.note_path == "shed.md" for h in hits)
