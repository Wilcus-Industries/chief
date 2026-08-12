"""Hybrid index over an Obsidian vault: one SQLite file, both retrieval halves.

Chunks are embedded with a cached model2vec static model and written into a
single SQLite database at ``index_home`` — always *outside* the vault, so
indexing never litters the notes. The file is keyed to the vault path, so one
index home can hold several vaults. Heavy imports (sqlite-vec, model2vec)
happen lazily inside the methods that touch them.

**Why one store.** Retrieval has two halves: meaning (``vec0`` cosine kNN) and
literal text (FTS5 + BM25). Semantic-only recall fails on exactly what a
personal vault is full of — project names, people, tools: a static embedding of
a token the model never saw is near-noise, so the note ranks nowhere while a
literal match finds it instantly. Two separate stores would drift, and the
self-heal below derives its plan from the index's own metadata — it would be
structurally blind to a note missing from only one of them. Instead all three
tables move inside a single transaction in :meth:`_index_note`. That
transaction *is* the sync guarantee: no triggers, no reconciliation pass.

``search`` fuses both halves by reciprocal-rank fusion; ``semantic`` and
``grep`` are the narrow verbs for when the caller knows which half it wants.
``ambient_candidates`` is the recall hook's own selection — same two halves,
but reserving slots rather than ranking, for the reason given there.
"""

import hashlib
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from chief_obsidian_memory import heal
from chief_obsidian_memory.chunk import chunk_note, iter_notes
from chief_obsidian_memory.config import MemorySettings
from chief_obsidian_memory.embedding import embed, load_model
from chief_obsidian_memory.storelock import StoreLock

# Which half of the index produced a hit. Carried through to the relevance gate
# and the CLI, so a literal match is judged on its own merits rather than as a
# weak vector neighbour.
SEMANTIC = "sem"
KEYWORD = "kw"
BOTH = "both"

# ponytail: the standard RRF constant; tune only if fusion visibly misranks.
_RRF_K = 60

# Each modality is asked for this many times ``k`` chunks, so that dedup by
# note_path still leaves enough distinct notes to fill ``k`` slots.
_OVERSAMPLE = 3

_TERM = re.compile(r"\w+")

# meta/chunks/fts only: ``chunks_vec`` needs the model's dimensionality, so it
# is created by the rebuild path once that is known.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chunks (
  id           INTEGER PRIMARY KEY,
  note_path    TEXT NOT NULL,
  heading      TEXT NOT NULL,
  text         TEXT NOT NULL,
  mtime        REAL NOT NULL,
  content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_by_note ON chunks(note_path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, heading);
"""


class SearchHit(NamedTuple):
    """One search result: the note it came from, its heading, the chunk text, a
    score (higher is better), and which half of the index found it."""

    note_path: str
    heading: str
    text: str
    score: float
    source: str = SEMANTIC


class VaultIndex:
    """A vault's chunks in one SQLite file under ``index_home``.

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
        self._db: sqlite3.Connection | None = None
        # The daemon hook and the CLI share this store across processes; the
        # public entry points hold this flock so a CLI reindex can't race the
        # hook's build/heal into a corrupted store.
        self._lock = StoreLock(self._index_home)

    def build(self) -> int:
        """(Re)build the whole index from the vault; returns the chunk count.

        Every table is emptied first so removed notes and headings don't
        linger. Raises ``FileNotFoundError`` if the vault path is missing, so a
        misconfigured vault fails loudly instead of yielding empty recall."""
        if not self._vault.is_dir():
            raise FileNotFoundError(f"vault path does not exist: {self._vault}")
        with self._lock:
            self._reset_store()
            count = 0
            for rel, text in iter_notes(self._vault, self._settings):
                count += self._index_note(rel, text)
            return count

    def search(self, query: str, k: int) -> list[SearchHit]:
        """Return the ``k`` best notes for ``query``, meaning and literal
        matches fused into one ranking, best first.

        Self-heals a missing/emptied index by building it, then runs the cheap
        mtime-gated sweep (:meth:`refresh`) so new, edited, and deleted notes are
        reconciled before the query — recall never needs a manual reindex, and
        never serves stale or empty results silently. The sweep is skippable via
        ``auto_refresh`` and rate-limited by ``refresh_min_interval_s``. A
        missing vault surfaces loudly from :meth:`build`."""
        if k <= 0:
            return []
        with self._lock:
            if self._prepare() is None:
                return []
            return _fuse(
                self._semantic(query, k * _OVERSAMPLE),
                self._keyword(query, k * _OVERSAMPLE),
                k,
            )

    def semantic(self, query: str, k: int) -> list[SearchHit]:
        """The meaning half alone: the ``k`` chunks closest to ``query`` by
        cosine distance. Same self-heal and sweep as :meth:`search`."""
        with self._lock:
            if self._prepare() is None:
                return []
            return self._semantic(query, k)

    def grep(self, query: str, k: int) -> list[SearchHit]:
        """The literal half alone: the ``k`` chunks best matching ``query`` as
        text, by BM25. Same self-heal and sweep as :meth:`search`."""
        with self._lock:
            if self._prepare() is None:
                return []
            return self._keyword(query, k)

    def ambient_candidates(self, query: str, k: int) -> list[SearchHit]:
        """The ``k`` candidates the ambient recall hook spends its gate calls on.

        Not :meth:`search`. That verb ranks, because something reads its list in
        order; this one covers, because every slot is a gate call already paid
        for and the gate judges each candidate alone. So half the slots are
        reserved per half (:func:`_reserve`) rather than left to fusion — a
        strong vector query must not be able to spend all four on meaning and
        leave the proper-noun case unrepresented."""
        if k <= 0:
            return []
        with self._lock:
            if self._prepare() is None:
                return []
            return _reserve(
                self._semantic(query, k * _OVERSAMPLE),
                self._keyword(query, k * _OVERSAMPLE),
                k,
            )

    def refresh(self) -> int:
        """Re-embed only the notes whose mtime advanced and bytes changed, index
        new notes, and drop deleted ones; returns how many notes were touched.
        The manual counterpart to the in-search sweep — always runs, ignoring
        ``auto_refresh``/throttle. Cheap: an untouched note costs one ``stat``."""
        with self._lock:
            return self._sweep_now()

    # --- read path --------------------------------------------------------

    def _prepare(self) -> sqlite3.Connection | None:
        """Self-heal, sweep, and hand back the connection — or ``None`` when the
        index is genuinely empty (an empty vault), which every verb answers with
        no hits. Assumes the store lock is held."""
        self._ensure()
        self._maybe_sweep()
        conn = self._open()
        return conn if self._count(conn) else None

    def _semantic(self, query: str, k: int) -> list[SearchHit]:
        if k <= 0:
            return []
        from sqlite_vec import serialize_float32

        rows = self._open().execute(
            "SELECT c.note_path, c.heading, c.text, v.distance "
            "FROM chunks_vec v JOIN chunks c ON c.id = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (serialize_float32(self._embed([query])[0]), k),
        ).fetchall()
        # vec0 is declared with a cosine metric, so distance maps back onto the
        # similarity score the CLI prints.
        return [
            SearchHit(path, heading, text, 1.0 - distance, SEMANTIC)
            for path, heading, text, distance in rows
        ]

    def _keyword(self, query: str, k: int) -> list[SearchHit]:
        match = _fts_query(query)
        if k <= 0 or not match:
            return []
        rows = self._open().execute(
            "SELECT chunks.note_path, chunks.heading, chunks.text, "
            "bm25(chunks_fts) FROM chunks_fts "
            "JOIN chunks ON chunks.id = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
            (match, k),
        ).fetchall()
        # BM25 is a cost (more negative is better); negate so every verb in
        # this module returns "higher is better". A score of exactly 0 means the
        # chunk matched only terms with no rarity value — every note holding
        # "the" against an OR-joined sentence. Those are not matches, and
        # keeping them lets a plateau of them crowd out the one real hit.
        return [
            SearchHit(path, heading, text, -rank, KEYWORD)
            for path, heading, text, rank in rows
            if rank < 0
        ]

    # --- freshness --------------------------------------------------------

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
        conn = self._ensure()
        to_reindex, to_delete = heal.sweep_plan(
            self._vault, self._settings, heal.indexed_meta(conn)
        )
        for rel in to_delete:
            self._delete_note(rel)
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
        # Per-vault (keyed on the store name), so one vault's sweep never
        # throttles another sharing the same index home.
        return self._index_home / f"{self._collection_name()}.sweep"

    def _ensure(self) -> sqlite3.Connection:
        """Open the store, rebuilding the whole index when it is empty, was
        dropped/corrupted, or was embedded with a different model — the
        self-heal path. Before, a changed ``embed_model`` silently answered with
        garbage until someone ran a manual reindex."""
        conn = self._open()
        if self._count(conn) == 0 or self._stale_model(conn):
            self.build()
            conn = self._open()
        return conn

    def _reindex_note(self, rel: str) -> None:
        self._delete_note(rel)
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
        digest = heal.content_hash(text)
        vectors = self._embed([c.text for c in chunks])
        from sqlite_vec import serialize_float32

        conn = self._open()
        # One transaction across all three tables. This is the entire sync
        # guarantee of the single-store design: a chunk cannot exist in the
        # keyword index but not the vector one, so the self-heal never has to
        # reconcile them against each other.
        with conn:
            for chunk, vector in zip(chunks, vectors, strict=True):
                row_id = conn.execute(
                    "INSERT INTO chunks(note_path, heading, text, mtime, "
                    "content_hash) VALUES (?, ?, ?, ?, ?)",
                    (rel, chunk.heading, chunk.text, mtime, digest),
                ).lastrowid
                conn.execute(
                    "INSERT INTO chunks_fts(rowid, text, heading) "
                    "VALUES (?, ?, ?)",
                    (row_id, chunk.text, chunk.heading),
                )
                conn.execute(
                    "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                    (row_id, serialize_float32(vector)),
                )
        return len(chunks)

    def _delete_note(self, rel: str) -> None:
        conn = self._open()
        with conn:
            ids = [
                (row[0],)
                for row in conn.execute(
                    "SELECT id FROM chunks WHERE note_path = ?", (rel,)
                )
            ]
            # The virtual tables carry no foreign key, so their rows are dropped
            # explicitly — in the same transaction, for the reason above.
            conn.executemany("DELETE FROM chunks_fts WHERE rowid = ?", ids)
            conn.executemany("DELETE FROM chunks_vec WHERE rowid = ?", ids)
            conn.execute("DELETE FROM chunks WHERE note_path = ?", (rel,))

    # --- store ------------------------------------------------------------

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return embed(self._get_model(), texts)

    def _get_model(self) -> Any:
        if self._model is None:
            self._model = load_model(self._settings.embed_model)
        return self._model

    def _open(self) -> sqlite3.Connection:
        if self._db is None:
            import sqlite_vec

            self._index_home.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path())
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            # WAL: the CLI and the daemon hook open the same file from separate
            # processes (the store flock serializes their writes, not their
            # reads).
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._db = conn
        return self._db

    def _reset_store(self) -> None:
        """Empty every table and recreate the vector one at the current model's
        dimensionality — a changed ``embed_model`` makes the old vectors
        meaningless, and may change the column width outright. Assumes the store
        lock is held (build holds it)."""
        dim = len(self._embed(["x"])[0])
        conn = self._open()
        with conn:
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM chunks_fts")
            conn.execute("DROP TABLE IF EXISTS chunks_vec")
            conn.execute(
                "CREATE VIRTUAL TABLE chunks_vec USING "
                f"vec0(embedding float[{dim}] distance_metric=cosine)"
            )
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (
                    ("embed_model", self._settings.embed_model),
                    ("dim", str(dim)),
                    ("vault_path", str(self._vault.resolve())),
                ),
            )

    def _count(self, conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0])

    def _stale_model(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'embed_model'"
        ).fetchone()
        return row is None or row[0] != self._settings.embed_model

    def _db_path(self) -> Path:
        return self._index_home / f"{self._collection_name()}.db"

    def _collection_name(self) -> str:
        digest = hashlib.sha256(str(self._vault.resolve()).encode()).hexdigest()
        return f"vault_{digest[:16]}"


def _fts_query(query: str) -> str:
    """An FTS5 ``MATCH`` expression for a natural-language query.

    Terms are **OR**-joined rather than FTS5's default AND, so a whole sentence
    still matches; BM25's rarity weighting then does the selecting — a stopword
    scores ≈0 while a rare proper noun dominates. That is why this needs no
    stopword list and no term extractor. Each term is double-quoted so vault
    punctuation (``-``, ``"``, ``*``) can never become FTS5 syntax and turn a
    search into a syntax error. A fully quoted input passes straight through as
    a phrase query, which is how a caller asks for an exact sequence."""
    stripped = query.strip()
    if len(stripped) > 1 and stripped.startswith('"') and stripped.endswith('"'):
        return stripped
    return " OR ".join(f'"{term}"' for term in _TERM.findall(stripped))


def _dedup(hits: list[SearchHit]) -> list[SearchHit]:
    """One hit per note, keeping its best-ranked chunk. Recall is about notes:
    two matching headings of one note must not eat two result slots."""
    best: dict[str, SearchHit] = {}
    for hit in hits:
        best.setdefault(hit.note_path, hit)
    return list(best.values())


def _rrf(
    semantic: list[SearchHit], keyword: list[SearchHit]
) -> tuple[dict[str, float], dict[str, set[str]]]:
    """Reciprocal-rank fusion over both halves: ``score = Σ 1/(60 + rank)``.

    Rank-based, so the halves' incomparable scales (cosine similarity vs BM25)
    never have to be normalized against each other, and a note both halves found
    outranks one only a single half did. Returns the score and the set of halves
    that found it, per note."""
    scores: dict[str, float] = {}
    sources: dict[str, set[str]] = {}
    for source, hits in ((SEMANTIC, semantic), (KEYWORD, keyword)):
        for rank, hit in enumerate(_dedup(hits), start=1):
            scores[hit.note_path] = scores.get(hit.note_path, 0.0) + 1.0 / (
                _RRF_K + rank
            )
            sources.setdefault(hit.note_path, set()).add(source)
    return scores, sources


def _fuse(
    semantic: list[SearchHit], keyword: list[SearchHit], k: int
) -> list[SearchHit]:
    """The ``k`` best notes by RRF score — plus one guarantee.

    RRF ranks by *position*, which discards the one thing BM25 knows: how
    decisive a match was. A vault of similarly worded notes — a daily-note
    template, a repeated heading — hands both halves a plateau of plausible
    near-misses carrying the query's ordinary words, and they out-fuse the
    single note that actually holds the rare proper noun. Measured on a vault
    where twenty notes share the target's phrasing, plain RRF drops that note
    out of the results entirely: the precise case this index exists for.

    So the best literal match keeps a slot when fusion would have dropped it.
    That is the whole correction — deliberately the smallest one that fixes it.
    Reserving a *share* of the slots for each half also works, but it spends
    them whether or not the keyword half earned them, evicting notes both
    halves agreed on from ordinary conceptual queries. Ranking is what this
    verb is for; :meth:`VaultIndex.ambient_candidates` is where coverage wins
    instead."""
    scores, sources = _rrf(semantic, keyword)
    best: dict[str, SearchHit] = {}
    for hit in _dedup(semantic) + _dedup(keyword):
        best.setdefault(hit.note_path, hit)
    ranked = [
        hit._replace(
            score=scores[hit.note_path], source=_source(sources[hit.note_path])
        )
        for hit in sorted(best.values(), key=lambda h: -scores[h.note_path])
    ]
    top = _dedup(keyword)[:1]
    if top and all(hit.note_path != top[0].note_path for hit in ranked[:k]):
        # Last slot, not first: fusion's ordering is still the better guide for
        # everything it did rank, and this note is here on one half's word.
        return ranked[: k - 1] + [
            hit for hit in ranked if hit.note_path == top[0].note_path
        ][:1]
    return ranked[:k]


def _reserve(
    semantic: list[SearchHit], keyword: list[SearchHit], k: int
) -> list[SearchHit]:
    """``k`` notes with half the slots reserved for each half, RRF-ordered.

    The ambient hook's policy, and a different objective from :func:`_fuse`:
    these are gate calls, already paid for, so coverage across both halves beats
    a finely ordered list. Reserving means a strong vector query cannot take
    every slot and leave the literal half — the half that catches proper nouns —
    unrepresented at the gate.

    When dedup collapses a reserved slot (both halves found the same note) the
    freed slot is backfilled from whichever half still has candidates, so a
    caller asking for ``k`` gets ``k`` whenever the vault can supply them."""
    scores, sources = _rrf(semantic, keyword)
    picked: list[SearchHit] = []
    seen: set[str] = set()

    def take(hits: list[SearchHit], upto: int) -> None:
        for hit in hits:
            if len(picked) >= upto:
                return
            if hit.note_path not in seen:
                seen.add(hit.note_path)
                picked.append(hit)

    take(_dedup(semantic), max(1, k // 2))  # the meaning half's reserved slots
    take(_dedup(keyword), k)  # the remaining slots, literal matches first
    take(_dedup(semantic), k)  # backfill whatever dedup collapsed
    return sorted(
        (
            hit._replace(
                score=scores[hit.note_path],
                source=_source(sources[hit.note_path]),
            )
            for hit in picked
        ),
        key=lambda hit: -hit.score,
    )


def _source(found_by: set[str]) -> str:
    return BOTH if len(found_by) > 1 else next(iter(found_by))
