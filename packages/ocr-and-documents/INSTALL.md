# Installing ocr-and-documents

Works on any platform. Gives chief text extraction from PDFs, DOCX, and
EPUBs (~25MB of wheels — light). Real OCR for scanned documents is **not**
part of this install; the skill covers when and how to offer it.

## Steps

1. **Add the Python dependencies (guarded self-edit).** With your file tools,
   add `pymupdf`, `pymupdf4llm`, and `python-docx` to
   `[project.dependencies]` in `pyproject.toml`, then run `uv sync` with the
   `shell` tool. (Import names are `pymupdf`, `pymupdf4llm`, `docx` — the
   manifest lists those.)
2. Run `bash packages/ocr-and-documents/install.sh` with the `shell` tool. It
   copies the skill verbatim to `skills/ocr-and-documents/` and records the
   install in the registry (`chief.registry_apply`). No config keys.
3. `restart` — the guarded commit brings the dependencies and skill live.
4. Verify: extract text from any PDF on disk (or generate one:
   `uv run python -c "import pymupdf; d=pymupdf.open(); p=d.new_page();
   p.insert_text((72,72),'hello'); d.save('/tmp/t.pdf')"` then extract it and
   confirm "hello" comes back).
5. Read the installed skill once — especially the scanned-PDF rule (offer
   OCR, never silently install it).
