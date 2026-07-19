"""Wikilink graph: 1-hop neighbours and graph-widened semantic recall."""

from pathlib import Path
from typing import Any

from chief_obsidian_memory.config import MemorySettings
from chief_obsidian_memory.graph import VaultGraph, related
from chief_obsidian_memory.index import VaultIndex


def test_links_returns_the_one_hop_neighbourhood(vault: Path) -> None:
    graph = VaultGraph(vault=vault, settings=MemorySettings())
    # roof.md links to [[contractors]] (and contractors links back).
    assert graph.neighbors("roof.md") == ["contractors.md"]
    # Accepts a bare stem too, and the link is bidirectional.
    assert graph.neighbors("contractors") == ["roof.md"]


def test_links_excludes_out_of_scope_notes(vault: Path) -> None:
    graph = VaultGraph(vault=vault, settings=MemorySettings())
    # garden links to tomatoes; neither templates/ nor attachments/ leak in.
    assert graph.neighbors("garden.md") == ["tomatoes.md"]


def test_related_combines_semantic_hit_with_a_graph_hop(
    vault: Path, embedder: Any
) -> None:
    index = VaultIndex(
        vault=vault,
        index_home=vault.parent / "index",
        settings=MemorySettings(),
        model=embedder,
    )
    index.build()
    graph = VaultGraph(vault=vault, settings=MemorySettings())

    # A pure k=1 semantic query lands on the roof note only; contractors.md is
    # about a building firm and its phone number, semantically far from the
    # query — yet it is one wikilink away, so `related` must surface it.
    semantic = [hit.note_path for hit in index.search("leaking roof", k=1)]
    assert semantic == ["roof.md"]
    assert "contractors.md" not in semantic

    widened = related(index, graph, "leaking roof", k=1, hops=1)
    assert widened[0] == "roof.md"
    assert "contractors.md" in widened
