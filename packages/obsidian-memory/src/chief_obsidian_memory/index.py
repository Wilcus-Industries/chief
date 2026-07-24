"""Persistent semantic index over an Obsidian vault (model2vec + chromadb).

Chunks are embedded with a cached model2vec static model and upserted into a
persistent chromadb collection stored at ``index_home`` — always *outside* the
vault, so indexing never litters the notes. The collection is keyed to the
vault path, so one index home can hold several vaults. Heavy imports (chromadb,
model2vec) happen lazily inside the methods that touch them.
"""

import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from chief_obsidian_memory import heal
from chief_obsidian_memory.chunk import chunk_note, iter_notes
from chief_obsidian_memory.config import MemorySettings
from chief_obsidian_memory.embedding import embed, load_model
from chief_obsidian_memory.storelock import StoreLock


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
        now: Callable[[], float] | None = None,
    ) -> None:
        self._vault = Path(vault)
        self._index_home = Path(index_home)
        self._settings = settings
        self._model = model
        # Clock behind the refresh throttle; injectable so tests drive it
        # without sleeping.
        self._now = now or time.time
        self._client: Any | None = None
        self._collection: Any | None = None
        # The daemon hook and the CLI share this store across processes; the
        # public entry points hold this flock so a CLI reindex can't race the
        # hook's build/heal into a corrupted collection.
        self._lock = StoreLock(self._index_home)

    def build(self) -> int:
        """(Re)build the whole index from the vault; returns the chunk count.

        The collection is dropped and recreated so removed notes and headings
        don't linger. Raises ``FileNotFoundError`` if the vault path is missing,
        so a misconfigured vault fails loudly instead of yielding empty recall."""
        if not self._vault.is_dir():
            raise FileNotFoundError(f"vault path does not exist: {self._vault}")
        with self._lock:
            self._reset_collection()
            count = 0
            for rel, text in iter_notes(self._vault, self._settings):
                count += self._index_note(rel, text)
            return count

    def search(self, query: str, k: int) -> list[SearchHit]:
        """Return the ``k`` chunks most similar to ``query``, closest first.

        Self-heals a missing/emptied index by building it, then runs the cheap
        mtime-gated sweep (:meth:`refresh`) so new, edited, and deleted notes are
        reconciled before the query — recall never needs a manual reindex, and
        never serves stale or empty results silently. The sweep is skippable via
        ``auto_refresh`` and rate-limited by ``refresh_min_interval_s``. A
        missing vault surfaces loudly from :meth:`build`."""
        with self._lock:
            self._ensure()
            self._maybe_sweep()
            collection = self._open()
            if collection.count() == 0:
                return []
            return self._query(query, k)

    def refresh(self) -> int:
        """Re-embed only the notes whose mtime advanced and bytes changed, index
        new notes, and drop deleted ones; returns how many notes were touched.
        The manual counterpart to the in-search sweep — always runs, ignoring
        ``auto_refresh``/throttle. Cheap: an untouched note costs one ``stat``."""
        with self._lock:
            return self._sweep_now()

    def _maybe_sweep(self) -> None:
        """Run the sweep on the search read-path, honoring the config policy:
        ``auto_refresh`` off skips it; a positive ``refresh_min_interval_s``
        skips it when the last sweep is within the window. Assumes the store
        lock is held (search holds it)."""
        if not self._settings.auto_refresh or self._throttled():
            return
        self._sweep_now()
        # Only the automatic path stamps: the throttle window tracks the last
        # in-search sweep. A manual `refresh`/`reindex` deliberately does not
        # reset it — at worst one redundant (idempotent) sweep follows.
        self._stamp_path().write_text(repr(self._now()))

    def _sweep_now(self) -> int:
        """The mtime-gated reconcile itself; assumes the store lock is held."""
        collection = self._ensure()
        to_reindex, to_delete = heal.sweep_plan(
            self._vault, self._settings, heal.indexed_meta(collection)
        )
        for rel in to_delete:
            collection.delete(where={"note_path": rel})
        for rel in to_reindex:
            self._reindex_note(rel)
        return len(to_reindex) + len(to_delete)

    def _throttled(self) -> bool:
        interval = self._settings.refresh_min_interval_s
        if interval <= 0:
            return False
        stamp = self._stamp_path()
        try:
            last = float(stamp.read_text())
        except (OSError, ValueError):
            return False  # no/garbled stamp — sweep and (re)write it
        return (self._now() - last) < interval

    def _stamp_path(self) -> Path:
        # Per-vault (keyed on the collection name), so one vault's sweep never
        # throttles another sharing the same index home.
        return self._index_home / f"{self._collection_name()}.sweep"

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
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            # Vanished between the sweep and here, or non-UTF-8: leave it
            # dropped rather than aborting the whole search; a later sweep
            # retries it. One bad note must never take down recall.
            return
        self._index_note(rel, text)

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
