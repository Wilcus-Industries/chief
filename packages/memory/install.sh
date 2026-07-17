#!/usr/bin/env bash
# Deterministic install for the memory package: copy its skill verbatim and
# seed an empty store. Memory is a discipline over the agent's own file + git
# tools, not new code — there is nothing to build or configure here.
# Interactive steps (read the skill once after install) stay in INSTALL.md.
#
# Runs inside the self-edit seatbelt (done-check + rollback); paths are
# relative to the repo root.
set -euo pipefail

src="packages/memory/skills/memory"
dst="skills/memory"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Seed the store once. Never clobber an existing MEMORY.md or its history.
store="data/memory"
mkdir -p "$store/facts"

index="$store/MEMORY.md"
if [ ! -f "$index" ]; then
  cat >"$index" <<'EOF'
# Memory index

One line per memory file: `- [Title](facts/<slug>.md) — one-line hook`.
This index loads every session; the fact files it points at do not. Keep the
hooks short — they are how a future session decides what to open.
EOF
fi

# The store is versioned in its own git repo (data/ is gitignored from the
# harness repo). Init once so every save can be committed; harmless if present.
if [ ! -d "$store/.git" ]; then
  git init -q "$store"
fi
