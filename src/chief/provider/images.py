"""Images in a transcript: stored as file paths, sent as data URLs.

A user message that carries photos keeps only their paths under
:data:`IMAGES_KEY`, so the persisted transcript stays small and a photo is one
file, not a base64 blob copied into every row. Only the call that actually
sends a model the transcript (``model_call``) loads them, as OpenAI-style
``image_url`` content parts; every other reader (token estimates, the
compaction summarizer, hooks) sees a ``[photo]`` stub instead.
"""

import base64
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IMAGES_KEY = "images"
# A flat per-image estimate for the compaction threshold: a ~1568px photo is
# about 1.6k tokens to Claude; counting its base64 (~400k chars) would trip
# compaction on the first picture.
IMAGE_TOKENS = 1_600
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # the provider-side cap for one image
MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
}
STUB = "[photo]"
MISSING = "[photo no longer available]"


def with_images(
    content: str, paths: list[str], load: bool
) -> str | list[dict[str, Any]]:
    """``content`` plus its photos: content parts when ``load``, else stubs."""
    if not load:
        return " ".join([content, *(STUB for _ in paths)]).strip()
    parts: list[dict[str, Any]] = [{"type": "text", "text": content or STUB}]
    for path in paths:
        url = data_url(Path(path))
        if url is None:
            parts.append({"type": "text", "text": MISSING})
        else:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def data_url(path: Path) -> str | None:
    """The image at ``path`` as a base64 data URL, or ``None`` when it is
    gone, unreadable, too big, or not a type a model takes."""
    media = MEDIA_TYPES.get(path.suffix.lower())
    if media is None:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        logger.warning("transcript image %s unreadable", path)
        return None
    if len(raw) > MAX_IMAGE_BYTES:
        logger.warning("transcript image %s over %d bytes", path, MAX_IMAGE_BYTES)
        return None
    return f"data:{media};base64,{base64.b64encode(raw).decode('ascii')}"


def image_count(messages: list[dict[str, Any]]) -> int:
    return sum(len(m.get(IMAGES_KEY) or ()) for m in messages)
