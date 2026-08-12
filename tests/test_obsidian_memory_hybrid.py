"""Hybrid retrieval: FTS5 keywords + sqlite-vec vectors, blended into one rank.

The motivating case is the rare proper noun, and it only reproduces *under
competition*. A four-note vault has nothing to crowd a match out, so semantic
search finds anything; a real vault has hundreds of similarly worded notes, and
the sentence embedding of a query whose one distinctive token is a project name
lands nearer a dozen of those than the note that actually names it. The
``crowded_vault`` fixture below is the smallest thing that reproduces that, and
``test_search_surfaces_a_rare_proper_noun_the_vector_half_loses`` is the feature.

The small shared ``vault`` and ``embedder`` fixtures (conftest) cover everything
that does not need scale.
"""

import random
from pathlib import Path
from typing import Any

import pytest
from chief_obsidian_memory.config import MemorySettings
from chief_obsidian_memory.index import (
    BOTH,
    KEYWORD,
    SEMANTIC,
    SearchHit,
    VaultIndex,
    _dedup,
    _rrf,
)

# An invented token no note but one contains — a stand-in for the project
# names, people, and tools a personal vault is mostly made of.
_RARE = "Voxwell"

# One sentence per topic, so the crowded vault occupies a realistically
# structured embedding space rather than uniform noise. Every tenth note shares
# the target note's phrasing, which is what makes the vector half lose it.
_TOPICS = (
    "The raised beds get morning sun and the {} seedlings need deep watering.",
    "Called the plumber about the {} pipe under the kitchen sink again.",
    "Booked flights for the {} trip in March and paid the deposit.",
    "The {} recipe needs an hour of resting before the dough is workable.",
    "Renewed the {} insurance policy; the premium went up by a fifth.",
    "Practiced the {} scales for twenty minutes before the lesson.",
    "The car needs new {} tyres before the winter sets in properly.",
    "Read a chapter of the {} book on the train and took notes.",
    "Signed the {} agreement for the north wing of the house.",
    "Weekly shop: {} bread, milk, eggs, coffee, and a bag of oranges.",
)
_FILLERS = (
    "tomato copper spring sourdough home piano front history builders rye"
).split()


@pytest.fixture
def crowded_vault(tmp_path: Path) -> Path:
    """200 notes of plausible, repetitive vault prose plus one note that names
    ``_RARE`` once — the competition a rare token has to survive."""
    vault = tmp_path / "crowded"
    vault.mkdir()
    rnd = random.Random(7)  # deterministic: the ranking assertions depend on it
    for i in range(200):
        topic = _TOPICS[i % len(_TOPICS)]
        body = " ".join(topic.format(rnd.choice(_FILLERS)) for _ in range(4))
        (vault / f"n{i:03d}.md").write_text(f"# Note {i}\n\n{body}\n")
    (vault / "vendors.md").write_text(
        "# Vendors\n\n"
        + " ".join(_TOPICS[8].format(rnd.choice(_FILLERS)) for _ in range(3))
        + f" {_RARE} is doing the work. "
        + " ".join(_TOPICS[9].format(rnd.choice(_FILLERS)) for _ in range(3))
        + "\n"
    )
    return vault


def _index(vault: Path, embedder: Any, **kwargs: Any) -> VaultIndex:
    return VaultIndex(
        vault=vault,
        index_home=vault.parent / "index",
        settings=MemorySettings(**kwargs),
        model=embedder,
    )


# --- the motivating case ----------------------------------------------------


def test_search_surfaces_a_rare_proper_noun_the_vector_half_loses(
    crowded_vault: Path, embedder: Any
) -> None:
    index = _index(crowded_vault, embedder)
    index.build()
    query = f"what did we agree with {_RARE} about the north wing"

    # The vector half loses it outright: 200 notes share the query's ordinary
    # vocabulary, and a static embedding cannot make one unseen token carry a
    # whole sentence. At the ambient hook's slot count it is not close.
    assert all(
        hit.note_path != "vendors.md" for hit in index.semantic(query, k=4)
    )

    # The literal half finds it instantly, and ranks it first by a clear BM25
    # margin over the notes that merely share the phrasing.
    literal = index.grep(query, k=5)
    assert literal[0].note_path == "vendors.md"
    assert literal[0].score > literal[1].score

    # Which is the whole point: the verb the agent and the ambient hook call
    # surfaces the note the vector half could not.
    hits = index.search(query, k=4)
    assert "vendors.md" in {hit.note_path for hit in hits}


def test_search_ranks_a_bare_rare_token_first(
    crowded_vault: Path, embedder: Any
) -> None:
    # Asked for the token alone, with nothing else to fuse on, the one note
    # containing it leads.
    index = _index(crowded_vault, embedder)
    index.build()
    assert index.search(_RARE, k=4)[0].note_path == "vendors.md"


def test_search_still_answers_a_conceptual_query(
    vault: Path, embedder: Any
) -> None:
    # The keyword half must not cost the meaning half: a question sharing
    # almost no vocabulary with the note still lands on it.
    index = _index(vault, embedder)
    index.build()
    hits = index.search("water leaking through the ceiling when it rains", k=4)
    assert hits[0].note_path == "roof.md"


# --- blending ---------------------------------------------------------------


def test_a_note_found_by_both_halves_outranks_one_half(
    vault: Path, embedder: Any
) -> None:
    index = _index(vault, embedder)
    index.build()
    hits = index.search("heirloom tomato seedlings want deep watering", k=4)
    assert hits[0].source == BOTH
    assert hits[0].note_path == "tomatoes.md"
    # RRF sums both halves' contributions, so one-half notes rank below.
    single = [hit for hit in hits if hit.source != BOTH]
    assert all(hits[0].score > hit.score for hit in single)


def test_search_keeps_a_slot_for_the_best_literal_match(
    crowded_vault: Path, embedder: Any
) -> None:
    # The one correction to plain RRF, and deliberately the smallest that
    # works. Fusion ranks by position, so a plateau of notes sharing the
    # query's ordinary words out-scores the single note holding the rare term;
    # the best literal match therefore keeps the last slot when fusion drops
    # it. Reserving a *share* of the slots would also fix this case, at the
    # cost of evicting agreed-on notes from ordinary queries — see
    # test_search_does_not_spend_slots_on_the_keyword_half.
    index = _index(crowded_vault, embedder)
    index.build()
    query = f"what did we agree with {_RARE} about the north wing"
    assert index.grep(query, k=1)[0].note_path == "vendors.md"
    hits = index.search(query, k=4)
    assert hits[-1].note_path == "vendors.md"
    assert len(hits) == len({hit.note_path for hit in hits}) == 4


def test_search_costs_plain_fusion_at_most_one_slot(
    crowded_vault: Path, embedder: Any
) -> None:
    """The guarantee is bounded: it may displace one result, never a quota.

    This is the property that makes it the *small* correction. A policy that
    reserved a share of the slots for each half could displace several — and
    would do so on every query, including conceptual ones where the keyword
    half earned nothing.
    """
    index = _index(crowded_vault, embedder)
    index.build()
    for query in (
        "how much did the insurance go up",
        "when do I need to water the young plants",
        f"what did we agree with {_RARE} about the north wing",
    ):
        got = {hit.note_path for hit in index.search(query, k=4)}
        plain = {hit.note_path for hit in _plain_rrf(index, query, k=4)}
        assert len(plain - got) <= 1, query


def _plain_rrf(index: VaultIndex, query: str, k: int) -> list[SearchHit]:
    """What unguaranteed reciprocal-rank fusion alone would have returned."""
    semantic = index.semantic(query, k * 3)
    keyword = index.grep(query, k * 3)
    scores, _ = _rrf(semantic, keyword)
    best: dict[str, SearchHit] = {}
    for hit in _dedup(semantic) + _dedup(keyword):
        best.setdefault(hit.note_path, hit)
    return sorted(best.values(), key=lambda h: -scores[h.note_path])[:k]


def test_search_dedups_by_note_path(vault: Path, embedder: Any) -> None:
    # roof.md has two headings ("Roof repair", "Budget") and both match this
    # query, but one note may only ever occupy one result slot.
    index = _index(vault, embedder)
    index.build()
    hits = index.search("roof repair budget dollars this autumn", k=4)
    paths = [hit.note_path for hit in hits]
    assert "roof.md" in paths
    assert len(paths) == len(set(paths))


# --- ambient slots: coverage, not ranking ----------------------------------


def test_ambient_candidates_reserve_slots_for_both_halves(
    crowded_vault: Path, embedder: Any
) -> None:
    # The hook's slots are gate calls already paid for, so they buy coverage
    # rather than a ranked list: the literal half is represented even when the
    # vector half would out-fuse it everywhere.
    index = _index(crowded_vault, embedder)
    index.build()
    hits = index.ambient_candidates(
        f"what did we agree with {_RARE} about the north wing", k=4
    )
    assert len(hits) == 4
    assert "vendors.md" in {hit.note_path for hit in hits}
    assert any(hit.source in (KEYWORD, BOTH) for hit in hits)
    assert any(hit.source in (SEMANTIC, BOTH) for hit in hits)


def test_ambient_candidates_backfill_when_dedup_collapses_a_slot(
    vault: Path, embedder: Any
) -> None:
    # Both halves agree on the same notes here, so the reserved slots collapse
    # on dedup; the freed slots must be spent, not dropped — every one is a
    # gate call the hook already paid for.
    index = _index(vault, embedder)
    index.build()
    paths = [
        hit.note_path for hit in index.ambient_candidates("roof repair budget", k=4)
    ]
    assert len(paths) == 4
    assert len(paths) == len(set(paths))


def test_ambient_candidates_cannot_exceed_the_notes_that_exist(
    vault: Path, embedder: Any
) -> None:
    index = _index(vault, embedder)
    index.build()
    paths = [hit.note_path for hit in index.ambient_candidates("roof", k=10)]
    assert len(paths) == len(set(paths)) == 4


def test_search_cannot_exceed_the_notes_that_exist(
    vault: Path, embedder: Any
) -> None:
    # Four in-scope notes: asking for more slots than the vault can fill
    # returns what exists rather than repeating a note to pad.
    index = _index(vault, embedder)
    index.build()
    paths = [hit.note_path for hit in index.search("roof", k=10)]
    assert len(paths) == len(set(paths)) == 4


# --- one store, one transaction ---------------------------------------------


def test_sweep_reconciles_the_keyword_and_vector_halves_together(
    vault: Path, embedder: Any
) -> None:
    # The single-store bet: a note added, edited, or deleted moves all three
    # tables in one transaction, so the halves cannot drift apart — and the
    # self-heal, which reads only `chunks`, stays able to repair either.
    index = _index(vault, embedder)
    index.build()
    assert _aligned(index)

    (vault / "vendors.md").write_text(
        f"# {_RARE}\n\nSigned the {_RARE} agreement for the north wing.\n"
    )
    assert index.refresh() >= 1
    assert _aligned(index)
    assert index.grep(_RARE, k=5)[0].note_path == "vendors.md"

    (vault / "vendors.md").unlink()
    assert index.refresh() >= 1
    assert _aligned(index)
    assert index.grep(_RARE, k=5) == []


def test_changing_the_embed_model_forces_a_rebuild(
    vault: Path, embedder: Any
) -> None:
    # Before, a changed embed_model silently answered with garbage until
    # someone ran a manual reindex. meta records the model the vectors were
    # built with, so a mismatch rebuilds on the next entry point instead.
    index = _index(vault, embedder)
    index.build()
    conn = index._open()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('embed_model', ?)",
            ("some/other-model",),
        )

    hits = index.search("water leaking through the ceiling", k=4)
    assert hits and hits[0].note_path == "roof.md"
    stored = conn.execute(
        "SELECT value FROM meta WHERE key = 'embed_model'"
    ).fetchone()[0]
    assert stored == MemorySettings().embed_model


def test_the_store_is_one_sqlite_file_outside_the_vault(
    vault: Path, embedder: Any
) -> None:
    index = _index(vault, embedder)
    index.build()
    assert index._db_path().is_file()
    assert index._db_path().parent == vault.parent / "index"
    assert list(vault.rglob("*.db")) == []


# --- lock coverage for the new verbs ----------------------------------------


def test_grep_and_semantic_take_the_lock_and_self_heal(
    vault: Path, tmp_path: Path, embedder: Any
) -> None:
    # Every public verb holds the store flock, and each self-heals an empty
    # index by building it (re-entering the lock rather than deadlocking).
    fresh = VaultIndex(
        vault=vault,
        index_home=tmp_path / "idx",
        settings=MemorySettings(),
        model=embedder,
    )
    assert fresh.grep("leaking roof in the rain", k=3)
    assert (tmp_path / "idx" / ".lock").exists()
    other = VaultIndex(
        vault=vault,
        index_home=tmp_path / "idx2",
        settings=MemorySettings(),
        model=embedder,
    )
    assert other.semantic("leaking roof in the rain", k=3)


# --- FTS5 query construction ------------------------------------------------


def test_grep_survives_punctuation_and_matches_a_whole_sentence(
    vault: Path, embedder: Any
) -> None:
    # Terms are OR-joined and individually quoted: a full sentence still
    # matches (FTS5 defaults to AND) and vault punctuation never becomes FTS5
    # syntax — an unquoted `-` or `"` is a query error, not a search.
    index = _index(vault, embedder)
    index.build()
    assert index.grep('the roof -- "leaks" when it rains!', k=5)
    assert index.grep("!!!", k=5) == []


def test_grep_treats_a_quoted_query_as_a_phrase(
    vault: Path, embedder: Any
) -> None:
    index = _index(vault, embedder)
    index.build()
    assert index.grep('"cracked and need replacing"', k=5)
    assert index.grep('"replacing need and cracked"', k=5) == []


def _aligned(index: VaultIndex) -> bool:
    """True when all three tables hold a row per chunk — no half-written note."""
    conn = index._open()
    counts = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("chunks", "chunks_fts", "chunks_vec")
    }
    return len(set(counts.values())) == 1 and counts["chunks"] > 0
