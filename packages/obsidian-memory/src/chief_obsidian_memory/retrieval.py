"""Ranking and query construction: the pure half of retrieval.

No database, no vault, no I/O — the functions that turn two ranked lists into
one answer, and a natural-language query into an FTS5 expression. Split out of
``index.py`` because none of it touches ``VaultIndex``, and because the choices
here — how fusion is corrected, what counts as a real keyword match — are the
ones most likely to be revisited.
"""

import re
from typing import NamedTuple

# Which half of the index produced a hit. Carried through to the relevance gate
# and the CLI, so a literal match is judged on its own merits rather than as a
# weak vector neighbour.
SEMANTIC = "sem"
KEYWORD = "kw"
BOTH = "both"

# ponytail: the standard RRF constant; tune only if fusion visibly misranks.
_RRF_K = 60

# Below this, a BM25 score is noise rather than a match. FTS5 clamps a term's
# IDF to 1e-6 instead of 0 (fts5_aux.c), so a chunk matching only ubiquitous
# words — every note holding "the" against an OR-joined sentence — still scores
# fractionally above zero, and a `> 0` test would keep every one of them.
# Measured, the two populations sit orders of magnitude apart: real matches
# score 3e-1 to 5e0, pure clamp noise 1e-6 to 3e-6 whatever the vault size.
_BM25_FLOOR = 1e-4

_TERM = re.compile(r"\w+")


class SearchHit(NamedTuple):
    """One search result: the note it came from, its heading, the chunk text, a
    score (higher is better), and which half of the index found it."""

    note_path: str
    heading: str
    text: str
    score: float
    # No default: the gate now weighs this label, so an omission must be a type
    # error rather than a silent claim that a keyword hit matched by meaning.
    source: str


def _fts_query(query: str) -> str:
    """An FTS5 ``MATCH`` expression for a natural-language query.

    Terms are **OR**-joined rather than FTS5's default AND, so a whole sentence
    still matches; BM25's rarity weighting then does the selecting — a stopword
    scores ≈0 while a rare proper noun dominates. That is why this needs no
    stopword list and no term extractor. Each term is double-quoted so vault
    punctuation (``-``, ``"``, ``*``) can never become FTS5 syntax and turn a
    search into a syntax error.

    One input passes straight through: a *single balanced phrase*, which is how
    a caller asks for an exact sequence. The balance check is the whole safety
    property — "starts and ends with a quote" also describes ``"a" OR "`` and
    ``"foo": "bar"``, which are FTS5 syntax and a syntax error respectively, and
    this text can arrive straight from an owner's chat message via the ambient
    hook. Anything else falls through to the term extractor below."""
    stripped = query.strip()
    if (
        len(stripped) > 1
        and stripped.startswith('"')
        and stripped.endswith('"')
        and stripped.count('"') == 2
    ):
        return stripped
    return " OR ".join(f'"{term}"' for term in _TERM.findall(stripped))


def _dedup(hits: list[SearchHit]) -> list[SearchHit]:
    """One hit per note, keeping its best-ranked chunk. Recall is about notes:
    two matching headings of one note must not eat two result slots."""
    best: dict[str, SearchHit] = {}
    for hit in hits:
        best.setdefault(hit.note_path, hit)
    return list(best.values())


def _significant(hits: list[SearchHit]) -> list[SearchHit]:
    """Literal hits carrying real signal, for the blended verbs only.

    A chunk that matched nothing but ubiquitous words is not evidence, and
    letting one through costs more than a missed hit: :func:`_fuse` guarantees
    the top literal match a slot, and the relevance gate is told an exact
    keyword match is strong on its own — so one noise hit becomes an arbitrary
    note, labelled trustworthy, on every common-word query.
    :meth:`VaultIndex.grep` deliberately does not filter: asked for literal
    matches, the honest answer on a small vault is the weak ones it has."""
    return [hit for hit in hits if hit.score >= _BM25_FLOOR]


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

    ``k <= 0`` returns nothing rather than negative-slicing into the whole list.

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
    if k <= 0:
        return []
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
    # k > 1 or there is no "last slot" to give away: at k == 1 the guarantee
    # would evict the fusion winner outright and make search a slower grep.
    if k > 1 and top and all(hit.note_path != top[0].note_path for hit in ranked[:k]):
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
    if k <= 0:
        return []
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

    # Floor division, not max(1, ...): at k == 1 the single slot belongs to the
    # literal half, which is the half this function exists to protect.
    take(_dedup(semantic), k // 2)  # the meaning half's reserved slots
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
