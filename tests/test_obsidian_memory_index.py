"""Indexer + embedding core: heading chunking, scope, real semantic search.

The ``embedder`` and ``vault`` fixtures (conftest) share one loaded model2vec
model and a throwaway vault copy so every test stays well under the timeout.
"""

from pathlib import Path
from typing import Any

from chief_obsidian_memory.chunk import chunk_note, iter_notes
from chief_obsidian_memory.config import MemorySettings
from chief_obsidian_memory.index import VaultIndex


def test_chunk_note_splits_one_chunk_per_heading() -> None:
    text = "# Roof repair\n\nleaky garage\n\n# Budget\n\nfour thousand dollars\n"
    chunks = chunk_note("roof.md", text)
    assert [c.heading for c in chunks] == ["Roof repair", "Budget"]
    assert all(c.note_path == "roof.md" for c in chunks)
    # The heading is carried into the embedded text so it informs the vector.
    assert chunks[0].text.startswith("Roof repair")
    assert "leaky garage" in chunks[0].text
    assert "four thousand" in chunks[1].text


def test_chunk_note_keeps_pre_heading_content_as_leading_chunk() -> None:
    chunks = chunk_note("n.md", "intro line\n\n# First\n\nbody\n")
    assert chunks[0].heading == ""
    assert "intro line" in chunks[0].text
    assert chunks[1].heading == "First"


def test_recall_defaults_are_tuned_small() -> None:
    # Tuned for the fast gate: a short transcript window and few candidates keep
    # each firing cheap (see the memory-relevance model default). Four rather
    # than two because retrieval is hybrid — search reserves half the slots for
    # literal matches, and two would leave each half a single candidate.
    settings = MemorySettings()
    assert settings.window == 10
    assert settings.top_k == 4


def test_auto_refresh_defaults_on_with_a_60s_throttle() -> None:
    # The in-search sweep is on by default; the 60s throttle keeps a burst of
    # searches over a large vault from re-scanning on every one.
    settings = MemorySettings()
    assert settings.auto_refresh is True
    assert settings.refresh_min_interval_s == 60


def test_auto_refresh_and_interval_parse_from_config() -> None:
    settings = MemorySettings.from_config(
        {"auto_refresh": False, "refresh_min_interval_s": 30}
    )
    assert settings.auto_refresh is False
    assert settings.refresh_min_interval_s == 30


def test_iter_notes_excludes_obsidian_templates_attachments(vault: Path) -> None:
    settings = MemorySettings()
    paths = {rel for rel, _ in iter_notes(vault, settings)}
    assert paths == {"roof.md", "contractors.md", "garden.md", "tomatoes.md"}
    assert not any(
        p.startswith((".obsidian/", "templates/", "attachments/")) for p in paths
    )


def test_build_indexes_only_in_scope_notes(vault: Path, embedder: Any) -> None:
    index = _index(vault, embedder)
    count = index.build()
    # Four notes, five headings total (roof has two) -> five chunks.
    assert count == 5


def test_semantic_search_returns_the_relevant_chunk(
    vault: Path, embedder: Any
) -> None:
    index = _index(vault, embedder)
    index.build()
    hits = index.search("water leaking through the ceiling when it rains", k=3)
    assert hits, "expected at least one hit"
    # The most similar chunk is the roof-repair note, not the garden notes.
    assert hits[0].note_path == "roof.md"
    assert hits[0].heading == "Roof repair"
    assert hits[0].score > hits[-1].score or len(hits) == 1


def test_index_home_lives_outside_the_vault(
    vault: Path, tmp_path: Path, embedder: Any
) -> None:
    index = _index(vault, embedder)
    index.build()
    # Indexing writes the SQLite store under index_home, never into the vault.
    assert list((tmp_path / "index").rglob("*")) != []
    assert list(vault.rglob("*.db")) == []
    assert list(vault.rglob("*.sqlite3")) == []
    assert {p.name for p in vault.iterdir()} >= {"roof.md", ".obsidian"}


def _index(vault: Path, embedder: Any) -> VaultIndex:
    return VaultIndex(
        vault=vault,
        index_home=vault.parent / "index",
        settings=MemorySettings(),
        model=embedder,
    )


# --- cross-process store lock (audit M3) ------------------------------------


def test_store_lock_excludes_a_second_holder_and_reenters(tmp_path: Path) -> None:
    import fcntl

    import pytest
    from chief_obsidian_memory.storelock import StoreLock

    lock = StoreLock(tmp_path / "idx")
    with lock:
        with lock:  # re-entrant: search's self-heal builds under the lock
            pass
        other = (tmp_path / "idx" / ".lock").open("w")
        with pytest.raises(OSError):
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        other.close()
    # Released on exit: a fresh holder acquires without blocking.
    other = (tmp_path / "idx" / ".lock").open("w")
    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(other.fileno(), fcntl.LOCK_UN)
    other.close()


def test_store_lock_closes_handle_when_flock_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # A flock failure (e.g. EINTR) must not leak the opened fd or corrupt the
    # depth count — the next enter should start clean (#234).
    import fcntl

    import pytest
    from chief_obsidian_memory.storelock import StoreLock

    lock = StoreLock(tmp_path / "idx")
    real_flock = fcntl.flock

    def boom(fd: int, op: int) -> None:
        raise OSError("interrupted")

    # storelock calls fcntl.flock through the shared module object, so
    # patching the fcntl module itself intercepts its call.
    monkeypatch.setattr(fcntl, "flock", boom)
    with pytest.raises(OSError):
        lock.__enter__()
    assert lock._handle is None
    assert lock._depth == 0
    # With flock restored the same instance acquires normally.
    monkeypatch.setattr(fcntl, "flock", real_flock)
    with lock:
        pass


def test_index_entry_points_take_the_lock_without_deadlock(
    vault: Path, tmp_path: Path, embedder: Any
) -> None:
    # build/refresh/search all hold the store flock; search's self-heal path
    # (empty collection -> build) re-enters it rather than deadlocking.
    settings = MemorySettings()
    index = VaultIndex(
        vault=vault, index_home=tmp_path / "idx", settings=settings, model=embedder
    )
    index.build()
    assert (tmp_path / "idx" / ".lock").exists()
    fresh = VaultIndex(
        vault=vault, index_home=tmp_path / "idx2", settings=settings, model=embedder
    )
    hits = fresh.search("leaking roof in the rain", 3)  # self-heal under lock
    assert hits
    assert fresh.refresh() == 0
