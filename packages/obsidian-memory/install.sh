#!/usr/bin/env bash
# Deterministic install for obsidian-memory: copy the skill verbatim and write
# the obsidian_memory config block. The interactive/customizable parts (vault
# mode, sync choice, layout, index scope, read-only vs writable paths) and the
# Python-dependency self-edit that pulls in the vector stack are NOT here — the
# agent does them from INSTALL.md.
#
# Parameters (env):
#   VAULT_PATH      absolute path to the Obsidian vault directory (required)
#   WRITABLE_PATHS  comma-separated, vault-relative dirs the agent may write to
#                   (optional; empty = read-only recall, capability follows this)
#
# The done-check gates the restart, but config.yaml is gitignored — a bad
# config write is NOT rolled back (the pre-restart config gate is the only
# protection). Paths are relative
# to the repo root.
set -euo pipefail

src="packages/obsidian-memory/skills/obsidian-memory"
dst="skills/obsidian-memory"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

: "${VAULT_PATH:?set VAULT_PATH to the Obsidian vault directory}"

# Build a YAML list of writable paths from the comma-separated input.
writable_yaml="["
if [ -n "${WRITABLE_PATHS:-}" ]; then
  IFS=',' read -ra parts <<<"$WRITABLE_PATHS"
  for path in "${parts[@]}"; do
    path="${path//[[:space:]]/}"
    [ -n "$path" ] && writable_yaml+="\"$path\","
  done
fi
writable_yaml+="]"

uv run python -m chief.config_apply \
  "obsidian_memory.vault_paths=[\"$VAULT_PATH\"]" \
  "obsidian_memory.writable_paths=$writable_yaml"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply obsidian-memory --source bundled
