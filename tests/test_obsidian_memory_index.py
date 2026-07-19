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
    # Indexing writes chromadb state under index_home, never into the vault.
    assert list((tmp_path / "index").rglob("*")) != []
    assert list(vault.rglob("*.sqlite3")) == []
    assert list(vault.rglob("*.bin")) == []
    assert {p.name for p in vault.iterdir()} >= {"roof.md", ".obsidian"}


def _index(vault: Path, embedder: Any) -> VaultIndex:
    return VaultIndex(
        vault=vault,
        index_home=vault.parent / "index",
        settings=MemorySettings(),
        model=embedder,
    )
