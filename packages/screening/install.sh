#!/usr/bin/env bash
# Deterministic install for the screening package: copy its skill verbatim.
# Screening is prompt policy, not code — there is nothing else to do here.
# Interactive steps (read the skill once after install) stay in INSTALL.md.
#
# The done-check gates the restart, but config.yaml is gitignored — a bad
# config write is NOT rolled back (the pre-restart config gate is the only
# protection). Paths are
# relative to the repo root.
set -euo pipefail

src="packages/screening/skills/screening"
dst="skills/screening"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply screening --source bundled
