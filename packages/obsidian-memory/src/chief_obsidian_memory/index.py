"""Persistent semantic index over an Obsidian vault (model2vec + chromadb).

Chunks are embedded with a cached model2vec static model and upserted into a
persistent chromadb collection stored at ``index_home`` — always *outside* the
vault, so indexing never litters the notes. The collection is keyed to the
vault path, so one index home can hold several vaults. Heavy imports (chromadb,
model2vec) happen lazily inside the methods that touch them.
"""

import hashlib
from pathlib import Path
from typing import Any, NamedTuple

from chief_obsidian_memory import heal
from chief_obsidian_memory.chunk import chunk_note, iter_notes
from chief_obsidian_memory.config import MemorySettings
from chief_obsidian_memory.embedding import embed, load_model


class SearchHit(NamedTuple):
    """One search result: the note it came from, its heading, the chunk text,
    and a cosine similarity in ``[0, 1]`` (higher is closer)."""

    note_path: str
    heading: str
    text: str
    score: float


class VaultIndex:
    """A vault's chunks in a persistent chromadb collection under ``index_home``.

    ``model`` may be injected (tests share one loaded model); otherwise it is
    lazily loaded and cached on first use.
    """

    def __init__(
        self,
        vault: Path,
        index_home: Path,
        settings: MemorySettings,
        model: Any | None = None,
    ) -> None:
        self._vault = Path(vault)
        self._index_home = Path(index_home)
        self._settings = settings
        self._model = model
        self._client: Any | None = None
        self._collection: Any | None = None

    def build(self) -> int:
        """(Re)build the whole index from the vault; returns the chunk count.

        The collection is dropped and recreated so removed notes and headings
        don't linger. Raises ``FileNotFoundError`` if the vault path is missing,
        so a misconfigured vault fails loudly instead of yielding empty recall."""
        if not self._vault.is_dir():
            raise FileNotFoundError(f"vault path does not exist: {self._vault}")
        self._reset_collection()
        count = 0
        for rel, text in iter_notes(self._vault, self._settings):
            count += self._index_note(rel, text)
        return count

    def search(self, query: str, k: int) -> list[SearchHit]:
        """Return the ``k`` chunks most similar to ``query``, closest first.

        Self-heals a missing/emptied index by building it, then does a cheap
        mtime staleness check on the hit notes and re-embeds any that drifted
        out of band before returning — so recall never serves stale or empty
        results silently. A missing vault surfaces loudly from :meth:`build`."""
        collection = self._ensure()
        if collection.count() == 0:
            return []
        hits = self._query(query, k)
        stale = heal.drifted(collection, self._vault, [h.note_path for h in hits])
        if stale:
            for rel in stale:
                self._reindex_note(rel)
            hits = self._query(query, k)
        return hits

    def refresh(self) -> int:
        """Re-embed only the notes whose content changed and drop deleted ones;
        returns how many notes were touched. Cheap enough for a cron sweep."""
        collection = self._ensure()
        to_reindex, to_delete = heal.diff(
            self._vault, self._settings, heal.indexed_hashes(collection)
        )
        for rel in to_delete:
            collection.delete(where={"note_path": rel})
        for rel in to_reindex:
            self._reindex_note(rel)
        return len(to_reindex) + len(to_delete)

    def _query(self, query: str, k: int) -> list[SearchHit]:
        collection = self._open()
        result = collection.query(
            query_embeddings=self._embed([query]),
            n_results=min(k, collection.count()),
        )
        return [
            SearchHit(meta["note_path"], meta["heading"], doc, 1.0 - dist)
            for doc, meta, dist in zip(
                result["documents"][0],
                result["metadatas"][0],
                result["distances"][0],
                strict=True,
            )
        ]

    def _ensure(self) -> Any:
        """Open the collection, building the whole index if it is empty or was
        dropped/corrupted — the self-heal path."""
        collection = self._open()
        if collection.count() == 0:
            self.build()
            collection = self._open()
        return collection

    def _reindex_note(self, rel: str) -> None:
        collection = self._open()
        collection.delete(where={"note_path": rel})
        path = self._vault / rel
        if path.exists():
            self._index_note(rel, path.read_text())

    def _index_note(self, rel: str, text: str) -> int:
        chunks = chunk_note(rel, text)
        if not chunks:
            return 0
        mtime = (self._vault / rel).stat().st_mtime
        collection = self._open()
        collection.upsert(
            ids=[f"{rel}::{i}" for i in range(len(chunks))],
            documents=[c.text for c in chunks],
            embeddings=self._embed([c.text for c in chunks]),
            metadatas=[
                {
                    "note_path": rel,
                    "heading": c.heading,
                    "mtime": mtime,
                    "content_hash": heal.content_hash(text),
                }
                for c in chunks
            ],
        )
        return len(chunks)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return embed(self._get_model(), texts)

    def _get_model(self) -> Any:
        if self._model is None:
            self._model = load_model(self._settings.embed_model)
        return self._model

    def _open(self) -> Any:
        if self._collection is None:
            self._collection = self._client_().get_or_create_collection(
                name=self._collection_name(), metadata={"hnsw:space": "cosine"}
            )
        return self._collection

    def _reset_collection(self) -> None:
        client = self._client_()
        name = self._collection_name()
        # A missing collection raises a version-specific error across chroma
        # releases; the intent is idempotent "drop if present", so swallow it.
        if name in {c.name for c in client.list_collections()}:
            client.delete_collection(name)
        self._collection = client.get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine"}
        )

    def _client_(self) -> Any:
        if self._client is None:
            import chromadb

            self._index_home.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self._index_home))
        return self._client

    def _collection_name(self) -> str:
        digest = hashlib.sha256(str(self._vault.resolve()).encode()).hexdigest()
        return f"vault_{digest[:16]}"
