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

from chief_obsidian_memory.chunk import chunk_note, iter_notes
from chief_obsidian_memory.config import MemorySettings


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
        """Return the ``k`` chunks most similar to ``query``, closest first."""
        collection = self._open()
        if collection.count() == 0:
            return []
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
                    "content_hash": _hash(text),
                }
                for c in chunks
            ],
        )
        return len(chunks)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._get_model().encode(texts)]

    def _get_model(self) -> Any:
        if self._model is None:
            from model2vec import StaticModel

            self._model = StaticModel.from_pretrained(self._settings.embed_model)
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


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
