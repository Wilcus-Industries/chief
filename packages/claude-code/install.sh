#!/usr/bin/env bash
# Deterministic install for claude-code: copy the skill verbatim. The CLI
# itself is a machine prerequisite (see INSTALL.md), deliberately NOT
# installed here — auth is interactive and owner-owned.
#
# Paths are relative to the repo root.
set -euo pipefail

# Warn (don't fail) if the CLI isn't visible — the skill refuses to run
# without it anyway, and PATH may differ between this shell and the daemon.
if ! command -v claude >/dev/null 2>&1; then
  echo "warning: 'claude' not found on PATH — install/auth it before" \
    "relying on this package (see INSTALL.md)" >&2
fi

src="packages/claude-code/skills/claude-code"
dst="skills/claude-code"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply claude-code --source bundled
