#!/usr/bin/env bash
# Deterministic install for the soul package: copy the skill verbatim and seed
# a starter Soul.md. No prompt wiring here or in INSTALL.md — the session inlines
# data/memory/Soul.md (via chief.agent.prompt.read_soul) at the top of the prompt
# every turn on its own, so seeding the file is the whole install.
#
# Depends on the memory package: Soul.md lives in that store (data/memory/) and
# is versioned by the same git repo. Runs inside the self-edit seatbelt; paths
# are relative to the repo root.
set -euo pipefail

src="packages/soul/skills/soul"
dst="skills/soul"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Seed a starter soul once. Never overwrite an existing Soul.md — that is the
# agent's own evolving self-concept.
store="data/memory"
mkdir -p "$store"
soul="$store/Soul.md"
if [ ! -f "$soul" ]; then
  cat >"$soul" <<'EOF'
# Soul

I am chief, a personal agent. I am candid, concise, and capable. I act on my
owner's behalf, keep their trust, and say plainly when I am unsure.

This file is my character — my voice, values, and boundaries. It is mine to
evolve as I learn how my owner wants me to be, and theirs to correct.
EOF
fi
