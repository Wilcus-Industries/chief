#!/usr/bin/env bash
# Deterministic install for youtube-content: copy the skill + fetch script
# verbatim. The Python-dependency self-edit (youtube-transcript-api in
# pyproject) is NOT here — the agent does it from INSTALL.md.
#
# Paths are relative to the repo root.
set -euo pipefail

src="packages/youtube-content/skills/youtube-content"
dst="skills/youtube-content"
mkdir -p "$dst/scripts"
cp "$src/SKILL.md" "$dst/SKILL.md"
cp packages/youtube-content/scripts/fetch_transcript.py "$dst/scripts/"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply youtube-content --source bundled
