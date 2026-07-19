"""Freshness helpers: diff the vault against the index to re-embed only changes.

Kept out of ``index.py`` so that module stays focused on embedding and querying.
Re-embedding is decided by content hash — a touch that leaves the bytes
unchanged never re-embeds — while mtime is the cheap query-time drift signal
(a ``stat``, not a read) used to catch an out-of-band edit at search time.
"""

import hashlib
from pathlib import Path
from typing import Any

from chief_obsidian_memory.chunk import iter_notes
from chief_obsidian_memory.config import MemorySettings


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def indexed_hashes(collection: Any) -> dict[str, str]:
    """note_path -> stored content hash, one entry per indexed note."""
    got = collection.get(include=["metadatas"])
    return {m["note_path"]: m["content_hash"] for m in got["metadatas"] or []}


def diff(
    vault: Path, settings: MemorySettings, indexed: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Diff disk against the index.

    Returns ``(to_reindex, to_delete)``: notes whose content changed or that are
    newly in scope, and indexed notes no longer present on disk or in scope."""
    on_disk: set[str] = set()
    to_reindex: list[str] = []
    for rel, text in iter_notes(vault, settings):
        on_disk.add(rel)
        if indexed.get(rel) != content_hash(text):
            to_reindex.append(rel)
    to_delete = [rel for rel in indexed if rel not in on_disk]
    return to_reindex, to_delete


def drifted(collection: Any, vault: Path, hit_paths: list[str]) -> list[str]:
    """Of ``hit_paths``, the notes whose on-disk mtime is newer than indexed (or
    that vanished) — the cheap query-time staleness check over just the hits."""
    unique = list(dict.fromkeys(hit_paths))
    got = collection.get(
        where={"note_path": {"$in": unique}}, include=["metadatas"]
    )
    indexed_mtime = {m["note_path"]: m["mtime"] for m in got["metadatas"] or []}
    stale: list[str] = []
    for rel in unique:
        path = vault / rel
        if not path.exists() or path.stat().st_mtime > indexed_mtime.get(rel, 0.0):
            stale.append(rel)
    return stale
