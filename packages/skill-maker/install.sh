#!/usr/bin/env bash
# Deterministic install for skill-maker: copy the skill verbatim. Prompt
# policy only — nothing else to do.
#
# Paths are relative to the repo root.
set -euo pipefail

src="packages/skill-maker/skills/skill-maker"
dst="skills/skill-maker"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply skill-maker --source bundled
