#!/usr/bin/env bash
# Deterministic install for apple-notes: install the memo CLI and copy the
# skill verbatim. The interactive part (the Automation → Notes permission
# prompt on first use) is NOT here — the agent walks it in INSTALL.md.
#
# Paths are relative to the repo root. macOS-only (requires Homebrew).
set -euo pipefail

# memo — the CLI chief drives (via the shell tool) to manage Apple Notes.
# Idempotent: skip if already on PATH.
if ! command -v memo >/dev/null 2>&1; then
  brew install antoniorodr/memo/memo
fi

src="packages/apple-notes/skills/apple-notes"
dst="skills/apple-notes"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply apple-notes --source bundled
