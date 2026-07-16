#!/usr/bin/env bash
# Deterministic install for the screening package: copy its skill verbatim.
# Screening is prompt policy, not code — there is nothing else to do here.
# Interactive steps (read the skill once after install) stay in INSTALL.md.
#
# Runs inside the self-edit seatbelt (done-check + rollback); paths are
# relative to the repo root.
set -euo pipefail

src="packages/screening/skills/screening"
dst="skills/screening"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"
