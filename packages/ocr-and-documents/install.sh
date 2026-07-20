#!/usr/bin/env bash
# Deterministic install for ocr-and-documents: copy the skill verbatim. The
# Python-dependency self-edit (pymupdf/pymupdf4llm/python-docx in pyproject)
# is NOT here — the agent does it from INSTALL.md, guarded by the done-check.
#
# Paths are relative to the repo root.
set -euo pipefail

src="packages/ocr-and-documents/skills/ocr-and-documents"
dst="skills/ocr-and-documents"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply ocr-and-documents --source bundled
