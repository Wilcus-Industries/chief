"""PDF pre-extraction on intake (#81, part of #72).

Builds a *real* minimal PDF with extractable text and runs it through the real
``pypdf`` path — the central mechanism — so the assertion proves genuine PDF-to-text,
not a mock. Also covers the intake transform that folds PDF text into the turn while
letting images pass through.
"""

from chief.adapters.base import Attachment
from chief.core.pdf import extract_pdf_attachments, extract_pdf_text


def make_pdf(text: str) -> bytes:
    """A minimal single-page PDF whose page shows ``text`` (extractable by pypdf)."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    ]
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode("latin-1") + b") Tj ET"
    objs.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream"
    )
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_pos = len(out)
    size = len(objs) + 1
    out += b"xref\n0 " + str(size).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size " + str(size).encode() + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_pos).encode() + b"\n%%EOF\n"
    )
    return out


def test_extract_pdf_text_reads_real_pdf() -> None:
    pdf = make_pdf("Hello chief PDF extraction")
    assert extract_pdf_text(pdf) == "Hello chief PDF extraction"


def test_extract_pdf_text_unreadable_returns_empty() -> None:
    # A non-PDF blob must not raise — intake degrades to a placeholder instead.
    assert extract_pdf_text(b"not a pdf at all") == ""


def test_extract_pdf_attachments_folds_text_and_drops_pdf() -> None:
    pdf = Attachment(
        media_type="application/pdf",
        data=make_pdf("Quarterly numbers"),
        filename="report.pdf",
    )
    text, attachments = extract_pdf_attachments("look at this", (pdf,))

    assert "look at this" in text
    assert "Quarterly numbers" in text
    assert "report.pdf" in text
    assert attachments == ()  # the PDF is consumed into text


def test_extract_pdf_attachments_keeps_images() -> None:
    image = Attachment(media_type="image/png", data=b"\x89PNG", filename="a.png")
    pdf = Attachment(
        media_type="application/pdf", data=make_pdf("doc body"), filename="d.pdf"
    )
    text, attachments = extract_pdf_attachments("hi", (image, pdf))

    assert "doc body" in text
    assert attachments == (image,)  # image passes through, PDF removed


def test_extract_pdf_attachments_no_pdf_is_noop() -> None:
    image = Attachment(media_type="image/png", data=b"\x89PNG")
    text, attachments = extract_pdf_attachments("hi", (image,))
    assert text == "hi"
    assert attachments == (image,)


def test_extract_pdf_attachments_scanned_pdf_placeholder() -> None:
    # A PDF with no extractable text still leaves a note so the model knows one arrived.
    blank = Attachment(
        media_type="application/pdf", data=b"broken", filename="scan.pdf"
    )
    text, attachments = extract_pdf_attachments("", (blank,))
    assert "scan.pdf" in text
    assert "no extractable text" in text
    assert attachments == ()
