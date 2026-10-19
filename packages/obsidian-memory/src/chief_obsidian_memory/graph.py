"""Wikilink graph over the vault: 1-hop neighbours and graph-expanded recall.

obsidiantools parses the ``[[wikilinks]]`` into a networkx graph whose nodes are
note stems; this wraps it in an undirected graph keyed by vault-relative note
paths (the same currency the index and CLI use) and filtered to in-scope notes.
``related`` widens a hybrid search with graph neighbours, surfacing linked
notes no query would rank on its own. obsidiantools/networkx import lazily.
"""

from pathlib import Path
from typing import Any

from chief_obsidian_memory.chunk import in_scope
from chief_obsidian_memory.config import MemorySettings
from chief_obsidian_memory.index import VaultIndex


class VaultGraph:
    """The vault's wikilink neighbourhoods, in vault-relative note paths."""

    def __init__(self, vault: Path, settings: MemorySettings) -> None:
        self._vault = Path(vault)
        self._settings = settings
        self._graph: Any | None = None
        self._stem_to_path: dict[str, str] = {}

    def neighbors(self, note: str, hops: int = 1) -> list[str]:
        """Note paths within ``hops`` wikilinks of ``note`` (itself excluded).

        ``note`` may be given as a note path (``roof.md``) or a bare stem
        (``roof``). Returns ``[]`` for an unknown or link-less note."""
        import networkx as nx

        graph = self._get()
        start = self._resolve(note)
        if start not in graph:
            return []
        reach = nx.single_source_shortest_path_length(graph, start, cutoff=hops)
        return sorted(other for other, dist in reach.items() if dist > 0)

    def _get(self) -> Any:
        if self._graph is None:
            self._graph = self._load()
        return self._graph

    def _load(self) -> Any:
        import networkx as nx
        import obsidiantools.api as otools

        parsed = otools.Vault(self._vault).connect().gather()
        self._stem_to_path = {
            stem: path.as_posix() for stem, path in parsed.md_file_index.items()
        }
        graph = nx.Graph()
        for source, target in parsed.graph.edges():
            src = self._stem_to_path.get(source)
            dst = self._stem_to_path.get(target)
            if src and dst and self._scoped(src) and self._scoped(dst):
                graph.add_edge(src, dst)
        return graph

    def _resolve(self, note: str) -> str:
        self._get()
        if note in self._stem_to_path.values():
            return note
        return self._stem_to_path.get(Path(note).stem, note)

    def _scoped(self, rel: str) -> bool:
        return in_scope(rel, self._settings)


def related(
    index: VaultIndex, graph: VaultGraph, query: str, k: int, hops: int = 1
) -> list[str]:
    """Search hits for ``query`` widened by their wikilink neighbours.

    Order is the hybrid search's hits first (best first), then each hit's graph
    neighbours within ``hops``, de-duplicated — so a linked note that neither
    half of the index would rank still surfaces."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(note_path: str) -> None:
        if note_path not in seen:
            seen.add(note_path)
            ordered.append(note_path)

    for hit in index.search(query, k):
        add(hit.note_path)
    for note_path in list(ordered):
        for neighbor in graph.neighbors(note_path, hops):
            add(neighbor)
    return ordered
