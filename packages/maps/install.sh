#!/usr/bin/env bash
# Deterministic install for maps: copy the skill + its bundled client script
# verbatim. Nothing interactive — no config, no secrets, stdlib-only.
#
# Paths are relative to the repo root.
set -euo pipefail

src="packages/maps/skills/maps"
dst="skills/maps"
mkdir -p "$dst/scripts"
cp "$src/SKILL.md" "$dst/SKILL.md"
cp packages/maps/scripts/maps_client.py "$dst/scripts/maps_client.py"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply maps --source bundled
