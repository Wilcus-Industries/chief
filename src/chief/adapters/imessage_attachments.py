"""Photos in iMessage rows: find them in ``chat.db``, stash them as JPEGs.

Only the owner's photos are ever stashed for a model to see: a picture can
carry instructions the text-only injection screen never reads, so someone
else's photo shows as ``[photo, not shown]`` and is never opened. The owner's
are converted with macOS ``sips`` (HEIC included) to a JPEG no longer than
:data:`MAX_EDGE` on its long side — what Claude reads at full detail — into
``data/attachments/imessage/``, once per attachment, and only when a turn or a
wake actually carries that row.
"""

import hashlib
import logging
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ATTACHMENT_QUERY = (
    "SELECT attachment.filename, attachment.mime_type, attachment.transfer_name, "
    "attachment.total_bytes FROM attachment "
    "JOIN message_attachment_join j ON j.attachment_id = attachment.ROWID "
    "WHERE j.message_id = ? ORDER BY attachment.ROWID"
)
OBJECT_MARK = "￼"  # where an attachment sits in a row's text
PHOTO = "[photo]"
NOT_SHOWN = "[photo, not shown]"
UNREADABLE = "[photo could not be read]"
MAX_EDGE = 1568
IMAGE_SUFFIXES = (".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".webp")
# An incoming photo's row can land before its file finishes downloading.
DOWNLOAD_WAIT_SECONDS = 15.0

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


@dataclass(frozen=True)
class Labelled:
    """A row's text with its photos labelled. ``owner_photos``: it has photos
    to stash; ``fingerprint`` names them stably across a row's twin copies."""

    text: str
    owner_photos: bool = False
    fingerprint: str = ""


class ImageStash:
    """Where the owner's photos land, and the lookups that find them."""

    def __init__(
        self, folder: Path, convert: Convert = sips_convert,
        wait: float = DOWNLOAD_WAIT_SECONDS,
    ) -> None:
        self._folder = folder
        self._convert = convert
        self._wait = wait

    def label(self, db_path: Path, rowid: int, text: str, owner: bool) -> Labelled:
        """``text`` with attachment marks turned into a label. Nothing is
        opened or converted here. Text without a mark passes through."""
        if OBJECT_MARK not in text:
            return Labelled(text)
        said = text.replace(OBJECT_MARK, "").strip()
        rows = self._images(db_path, rowid)
        if not rows:
            return Labelled(said)
        fingerprint = "|".join(f"{name}:{size}" for _, name, size in rows)
        label = PHOTO if owner else NOT_SHOWN
        return Labelled(" ".join([label, said]).strip(), owner, fingerprint)

    def stash(self, db_path: Path, rowid: int) -> tuple[str, ...]:
        """The row's photos as stashed JPEG paths. Call it for the owner's
        rows only (the caller knows who sent it)."""
        sources = [src for src, _, _ in self._images(db_path, rowid)]
        return tuple(p for i, src in enumerate(sources)
                     if (p := self._stash(db_path, rowid, i, src)))

    def _images(self, db_path: Path, rowid: int) -> list[tuple[Path, str, int]]:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(ATTACHMENT_QUERY, (rowid,)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning("attachments of row %s in %s unreadable: %s",
                           rowid, db_path, exc)
            return []
        home = db_path.parents[2] if len(db_path.parents) > 2 else Path.home()
        found = []
        for filename, mime, transfer, size in rows:
            name = str(filename or "")
            if name and (str(mime or "").startswith("image/")
                         or name.lower().endswith(IMAGE_SUFFIXES)):
                # "~" is the home of whoever owns this store, not chief's.
                path = home / name[2:] if name.startswith("~/") else Path(name)
                found.append((path, str(transfer or path.name), int(size or 0)))
        return found

    def _stash(self, db_path: Path, rowid: int, index: int, src: Path) -> str | None:
        store = hashlib.sha256(str(db_path).encode()).hexdigest()[:8]
        dst = self._folder / f"{store}-{rowid}-{index}.jpg"
        if dst.exists():
            return str(dst)
        deadline = time.monotonic() + self._wait
        while not src.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        try:
            self._folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("cannot create %s", self._folder)
            return None
        if not self._convert(src, dst):
            logger.warning("photo %s not stashed", src)
            return None
        return str(dst)
