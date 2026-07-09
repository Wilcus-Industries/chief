"""PDF pre-extraction on intake (#81, part of #72).

Both backends stop seeing PDF *content blocks*: claude-agent-sdk used to send an
incoming PDF as a native ``document`` block (``session._build_user_message``) and the
Copilot session dropped attachments entirely. Neither is portable, so
:meth:`chief.core.tasks.TaskManager.dispatch` — the one platform- and backend-agnostic
seam — extracts each incoming PDF to text here and folds it into the turn text, leaving
images to pass through untouched. The model then reads the PDF as plain text on either
backend.

Extraction is pure-Python (``pypdf``), so it needs no system libraries or sidecar; a PDF
with no extractable text (e.g. a pure scan — DESIGN M8 says no OCR) yields an empty body
that is surfaced as a short placeholder rather than silently dropping the attachment.
"""

import io
import logging

from pypdf import PdfReader

from ..adapters.base import Attachment

logger = logging.getLogger("chief.core.pdf")

PDF_MEDIA_TYPE = "application/pdf"


def extract_pdf_text(data: bytes) -> str:
    """Extract the concatenated page text from PDF ``data`` (empty if none/unreadable).

    Never raises: a malformed or image-only PDF returns ``""`` so intake degrades to a
    placeholder note instead of failing the whole turn.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        parts = [page.extract_text() or "" for page in reader.pages]
    except Exception:
        logger.warning("PDF text extraction failed", exc_info=True)
        return ""
    return "\n".join(parts).strip()


def extract_pdf_attachments(
    text: str, attachments: tuple[Attachment, ...]
) -> tuple[str, tuple[Attachment, ...]]:
    """Fold each incoming PDF's extracted text into ``text``; keep non-PDF attachments.

    Returns the augmented turn text and the attachments minus every PDF (images and any
    other media pass through). A PDF with no extractable text contributes a short
    placeholder so the model still knows a document arrived.
    """
    kept: list[Attachment] = []
    extracts: list[str] = []
    for att in attachments:
        if att.media_type == PDF_MEDIA_TYPE:
            label = att.filename or "attached PDF"
            body = extract_pdf_text(att.data)
            if body:
                extracts.append(f"[Extracted text from {label}]\n{body}")
            else:
                extracts.append(f"[{label}: no extractable text found]")
        else:
            kept.append(att)
    if extracts:
        joined = "\n\n".join(extracts)
        text = f"{text}\n\n{joined}" if text.strip() else joined
    return text, tuple(kept)
