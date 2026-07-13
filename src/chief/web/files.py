"""Scoped file access for the web UI (#153): workspace + screenshots, nothing else.

Deliberately NOT a disk browser. Exactly two areas exist — the agent workspace
(read/write: uploads land here) and the screenshots dir (read-only) — and every path
is fenced server-side: area names come off an allowlist, a requested path must
resolve inside its area root (symlink- and ``..``-proof via ``Path.resolve``), and an
uploaded filename is stripped to its basename. The browser's query string is treated
as hostile input throughout.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Cap on listed entries — the page is a workspace window, not an archive explorer.
LIST_LIMIT: Final[int] = 500

#: The one writable area: uploads may only ever land in the agent workspace.
UPLOAD_AREA: Final[str] = "workspace"


class UnknownAreaError(LookupError):
    """The requested area is not one of the exposed roots."""


class ForbiddenPathError(ValueError):
    """The requested path escapes (or never was inside) its area root."""


@dataclass(frozen=True)
class FileEntry:
    """One listed file: its area-relative posix path, size, and mtime (epoch s)."""

    path: str
    size: int
    modified: float


class FileAreas:
    """The exposed file roots, keyed by area name; ``None`` roots are simply absent
    (screenshots only exist when the playwright sidecar is configured)."""

    def __init__(self, *, workspace: Path | None, screenshots: Path | None) -> None:
        self._roots: dict[str, Path] = {}
        if workspace is not None:
            self._roots["workspace"] = workspace
        if screenshots is not None:
            self._roots["screenshots"] = screenshots

    def names(self) -> list[str]:
        """The available area names, workspace first."""
        return list(self._roots)

    def _root(self, area: str) -> Path:
        root = self._roots.get(area)
        if root is None:
            raise UnknownAreaError(f"no such file area: {area!r}")
        return root

    def list(self, area: str) -> list[FileEntry]:
        """The area's files (recursive), newest first, capped at LIST_LIMIT."""
        root = self._root(area)
        if not root.is_dir():
            return []
        entries = [
            FileEntry(
                path=candidate.relative_to(root).as_posix(),
                size=stat.st_size,
                modified=stat.st_mtime,
            )
            for candidate in root.rglob("*")
            if candidate.is_file() and not candidate.name.startswith(".")
            for stat in (candidate.stat(),)
        ]
        entries.sort(key=lambda e: e.modified, reverse=True)
        return entries[:LIST_LIMIT]

    def open_path(self, area: str, relative: str) -> Path:
        """Resolve one downloadable file, or raise on any escape attempt.

        The fence: the candidate is resolved (symlinks and ``..`` collapsed) and
        must still sit inside the resolved area root. Absolute inputs fail the
        same check naturally — ``root / "/abs"`` re-roots to ``/abs``, which is
        never inside the root.
        """
        root = self._root(area).resolve()
        candidate = (root / relative).resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise ForbiddenPathError(f"path escapes the {area} area: {relative!r}")
        if not candidate.is_file():
            raise ForbiddenPathError(f"no such file in {area}: {relative!r}")
        return candidate

    def save_upload(self, filename: str, data: bytes) -> Path:
        """Write an uploaded file into the workspace; returns the path written.

        The client-supplied name is reduced to its basename (no directories, no
        traversal); an empty or hidden name falls back to ``upload``; a collision
        gets a numeric suffix rather than clobbering the existing file.
        """
        root = self._root(UPLOAD_AREA)
        root.mkdir(parents=True, exist_ok=True)
        name = Path(filename).name
        if not name or name.startswith("."):
            name = "upload"
        target = root / name
        counter = 1
        while target.exists():
            target = root / f"{Path(name).stem}-{counter}{Path(name).suffix}"
            counter += 1
        target.write_bytes(data)
        return target
