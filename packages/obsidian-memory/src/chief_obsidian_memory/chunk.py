"""Heading-based chunking of Obsidian notes and scoped vault iteration.

A note is split at its Markdown headings: each heading plus the body beneath it
(down to the next heading) is one chunk, carrying its note path and heading as
metadata. Any content before the first heading becomes a leading, heading-less
chunk. Iteration honors the include/exclude path prefixes from the settings.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from chief_obsidian_memory.config import MemorySettings

_HEADING = re.compile(r"^#{1,6}\s+(.*)$")


@dataclass(frozen=True)
class Chunk:
    """One indexable section of a note: its vault-relative path (posix), the
    heading it falls under (empty for pre-heading content), and the composed
    text that gets embedded (heading + body, so the heading informs the vector)."""

    note_path: str
    heading: str
    text: str


def chunk_note(note_path: str, text: str) -> list[Chunk]:
    """Split ``text`` into one chunk per heading (plus a leading chunk for any
    pre-heading content). Empty sections are dropped."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        matched = _HEADING.match(line)
        if matched:
            sections.append((matched.group(1).strip(), []))
        else:
            sections[-1][1].append(line)
    chunks: list[Chunk] = []
    for heading, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not heading and not body:
            continue
        chunks.append(
            Chunk(note_path=note_path, heading=heading, text=_compose(heading, body))
        )
    return chunks


def _compose(heading: str, body: str) -> str:
    if heading and body:
        return f"{heading}\n\n{body}"
    return heading or body


def iter_note_paths(vault: Path, settings: MemorySettings) -> Iterator[str]:
    """Yield the vault-relative posix path of each in-scope ``.md`` note, without
    reading any file — the cheap walk the mtime-gated sweep stats over."""
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault).as_posix()
        if in_scope(rel, settings):
            yield rel


def iter_notes(vault: Path, settings: MemorySettings) -> Iterator[tuple[str, str]]:
    """Yield ``(vault-relative posix path, text)`` for each in-scope ``.md``
    note. ``include`` (if set) whitelists path prefixes; ``exclude`` drops
    them — so ``.obsidian/``, ``templates/`` and ``attachments/`` never index."""
    for rel in iter_note_paths(vault, settings):
        yield rel, (vault / rel).read_text()


def in_scope(rel: str, settings: MemorySettings) -> bool:
    """True when a vault-relative note path passes the include/exclude prefixes.

    Shared by iteration and the wikilink graph so both index the same notes."""
    if settings.include and not rel.startswith(settings.include):
        return False
    return not rel.startswith(settings.exclude)
