"""Freshness helpers: diff the vault against the index to re-embed only changes.

Kept out of ``index.py`` so that module stays focused on embedding and querying.
The sweep is *mtime-gated*: a note's on-disk mtime (a ``stat``, not a read) picks
which notes are candidates, and only those get read and content-hashed — so a
touch that leaves the bytes unchanged never re-embeds, and an untouched note
costs a single ``stat``. The one thing this trades away: a content change that
does not advance mtime (a git checkout, a timestamp-preserving sync) is missed
until a full ``reindex`` — the documented, accepted corner.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chief_obsidian_memory.chunk import iter_note_paths
from chief_obsidian_memory.config import MemorySettings


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class IndexedNote:
    """What the index remembers about a note: the mtime it was embedded at (the
    cheap staleness signal) and the hash of its bytes (the authoritative one)."""

    mtime: float
    content_hash: str


def indexed_meta(conn: Any) -> dict[str, IndexedNote]:
    """note_path -> its indexed mtime + content hash, one entry per note.

    A note has one row per chunk, all carrying the same mtime and hash;
    ``DISTINCT`` collapses them to one record each."""
    return {
        note_path: IndexedNote(mtime=mtime, content_hash=digest)
        for note_path, mtime, digest in conn.execute(
            "SELECT DISTINCT note_path, mtime, content_hash FROM chunks"
        )
    }


def sweep_plan(
    vault: Path, settings: MemorySettings, indexed: dict[str, IndexedNote]
) -> tuple[list[str], list[str]]:
    """Diff disk against the index, mtime-gated.

    Returns ``(to_reindex, to_delete)``: notes that are new, or whose mtime
    advanced past the index *and* whose bytes actually changed; and indexed
    notes no longer present on disk or in scope. Unchanged notes cost one
    ``stat`` and no read.

    Robust to a note that vanishes between the walk and the ``stat`` (an
    external sync or an editor delete — the store lock only serializes chief's
    own processes) or that won't decode: it is skipped, never raised, so one
    bad file can't abort the sweep and, with it, the whole search."""
    on_disk: set[str] = set()
    to_reindex: list[str] = []
    for rel in iter_note_paths(vault, settings):
        path = vault / rel
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue  # gone since the walk: left out of on_disk, so it drops
        on_disk.add(rel)
        record = indexed.get(rel)
        if record is None:
            to_reindex.append(rel)  # never indexed
            continue
        if mtime <= record.mtime:
            continue  # mtime unmoved: assume bytes unchanged (accepted corner)
        try:
            changed = content_hash(path.read_text()) != record.content_hash
        except (OSError, UnicodeDecodeError):
            continue  # unreadable right now: keep the existing index entry
        if changed:
            to_reindex.append(rel)
    to_delete = [rel for rel in indexed if rel not in on_disk]
    return to_reindex, to_delete
