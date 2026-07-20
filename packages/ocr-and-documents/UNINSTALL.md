# Uninstalling ocr-and-documents

1. Remove `pymupdf`, `pymupdf4llm`, and `python-docx` from
   `[project.dependencies]` in `pyproject.toml` (guarded self-edit) and run
   `uv sync`.
2. Delete the installed skill dir `skills/ocr-and-documents/`.
3. Deregister: `uv run python -m chief.registry_apply ocr-and-documents --remove`.
4. Delete this `UNINSTALL.md` (`packages/ocr-and-documents/UNINSTALL.md`) —
   its absence signals the uninstall completed.
5. `restart` to bring the change live.
