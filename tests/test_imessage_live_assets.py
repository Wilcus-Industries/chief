"""The self-DM live gate's media builders are structurally valid (#164).

The env-gated e2e suite (tests/test_imessage_live.py) sends a real image and a
real PDF through Messages and asserts a content-aware reply. Those payloads are
built dependency-free in tests/imessage_helpers.py; these tests pin their
structure (magic bytes, parseable dimensions, embedded text, xref offsets) so a
malformed asset fails here on any platform instead of as a confusing
Messages/vision failure on the rig.
"""

import struct
import zlib

from imessage_helpers import solid_png, tiny_pdf


class TestSolidPng:
    def test_has_png_magic_and_declared_dimensions(self) -> None:
        data = solid_png(255, 0, 0, size=64)

        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert data[12:16] == b"IHDR"
        width, height = struct.unpack(">II", data[16:24])
        assert (width, height) == (64, 64)

    def test_pixels_decompress_to_the_solid_color(self) -> None:
        size = 8
        data = solid_png(0, 128, 255, size=size)

        idat_at = data.index(b"IDAT")
        (length,) = struct.unpack(">I", data[idat_at - 4 : idat_at])
        raw = zlib.decompress(data[idat_at + 4 : idat_at + 4 + length])

        expected_row = b"\x00" + bytes((0, 128, 255)) * size
        assert raw == expected_row * size

    def test_chunk_crcs_are_correct(self) -> None:
        data = solid_png(1, 2, 3, size=4)

        at = 8
        while at < len(data):
            (length,) = struct.unpack(">I", data[at : at + 4])
            payload = data[at + 4 : at + 8 + length]
            (crc,) = struct.unpack(">I", data[at + 8 + length : at + 12 + length])
            assert crc == zlib.crc32(payload), payload[:4]
            at += 12 + length


class TestTinyPdf:
    def test_has_pdf_magic_embedded_text_and_eof(self) -> None:
        data = tiny_pdf("Codeword: MANGO42")

        assert data.startswith(b"%PDF-1.4\n")
        assert b"Codeword: MANGO42" in data
        assert data.rstrip().endswith(b"%%EOF")

    def test_startxref_points_at_the_xref_table(self) -> None:
        data = tiny_pdf("hello")

        tail = data[data.rindex(b"startxref") :]
        offset = int(tail.splitlines()[1])
        assert data[offset : offset + 4] == b"xref"

    def test_xref_offsets_point_at_their_objects(self) -> None:
        data = tiny_pdf("hello")

        xref_at = data.rindex(b"xref")
        lines = data[xref_at:].splitlines()[3:]  # skip xref, subsection, free entry
        for number, line in enumerate(lines, start=1):
            if not line.endswith(b"n "):
                break
            offset = int(line.split()[0])
            assert data[offset:].startswith(f"{number} 0 obj".encode())

    def test_text_with_parens_and_backslash_is_escaped(self) -> None:
        data = tiny_pdf(r"say (hi) \now")

        assert rb"say \(hi\) \\now" in data
