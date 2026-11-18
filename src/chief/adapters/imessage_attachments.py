"""Photos in iMessage rows: find them in ``chat.db``, stash them as JPEGs.

Only the owner's photos are ever stashed for a model to see: a picture can
carry instructions the text-only injection screen never reads, so someone
else's photo shows as ``[photo, not shown]`` and is never loaded. The owner's
are converted with macOS ``sips`` (HEIC included) to a JPEG no longer than
:data:`MAX_EDGE` on its long side — what Claude reads at full detail — into
``data/attachments/imessage/``, once per attachment.
"""

import hashlib
import logging
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

ATTACHMENT_QUERY = (
    "SELECT attachment.filename, attachment.mime_type FROM attachment "
    "JOIN message_attachment_join j ON j.attachment_id = attachment.ROWID "
    "WHERE j.message_id = ? ORDER BY attachment.ROWID"
)
OBJECT_MARK = "￼"  # where an attachment sits in a row's text
PHOTO = "[photo]"
NOT_SHOWN = "[photo, not shown]"
MAX_EDGE = 1568
IMAGE_SUFFIXES = (".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".webp")

Convert = Callable[[Path, Path], bool]


def sips_convert(src: Path, dst: Path) -> bool:
    """``src`` as a JPEG at ``dst``, long edge capped at :data:`MAX_EDGE`."""
    command = ["sips", "-s", "format", "jpeg", "-Z", str(MAX_EDGE),
               str(src), "--out", str(dst)]
    try:
        done = subprocess.run(command, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        logger.exception("sips could not convert %s", src)
        return False
    return done.returncode == 0 and dst.exists()


class ImageStash:
    """Where the owner's photos land, and the lookups that find them."""

    def __init__(self, folder: Path, convert: Convert = sips_convert) -> None:
        self._folder = folder
        self._convert = convert

    def resolve(
        self, db_path: Path, rowid: int, text: str, owner: bool
    ) -> tuple[str, tuple[str, ...]]:
        """A row's text with its attachment marks labelled, and the stashed
        photo paths (the owner's only). Text without a mark passes through."""
        if OBJECT_MARK not in text:
            return text, ()
        said = text.replace(OBJECT_MARK, "").strip()
        sources = self._sources(db_path, rowid)
        if not sources:
            return said, ()
        if not owner:
            return " ".join([NOT_SHOWN, said]).strip(), ()
        paths = tuple(p for i, src in enumerate(sources)
                      if (p := self._stash(db_path, rowid, i, src)))
        label = PHOTO if paths else "[photo could not be read]"
        return " ".join([label, said]).strip(), paths

    def _sources(self, db_path: Path, rowid: int) -> list[Path]:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute(ATTACHMENT_QUERY, (rowid,)).fetchall()
        except sqlite3.Error:
            return []  # an older store without attachment tables
        finally:
            conn.close()
        home = db_path.parents[2] if len(db_path.parents) > 2 else Path.home()
        sources = []
        for filename, mime in rows:
            name = str(filename or "")
            is_image = str(mime or "").startswith("image/") or name.lower().endswith(
                IMAGE_SUFFIXES)
            if name and is_image:
                # "~" is the home of whoever owns this store, not chief's.
                sources.append(home / name[2:] if name.startswith("~/") else Path(name))
        return sources

    def _stash(self, db_path: Path, rowid: int, index: int, src: Path) -> str | None:
        store = hashlib.sha256(str(db_path).encode()).hexdigest()[:8]
        dst = self._folder / f"{store}-{rowid}-{index}.jpg"
        if dst.exists():
            return str(dst)
        try:
            self._folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("cannot create %s", self._folder)
            return None
        if not self._convert(src, dst):
            logger.warning("photo %s not stashed", src)
            return None
        return str(dst)
