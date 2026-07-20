#!/usr/bin/env bash
# Deterministic install for watchers: copy the skill + the three polling
# scripts and their shared watermark helper verbatim, and create the state
# dir. Schedules are NOT created here — the agent wires those per-watcher
# from the skill, on the owner's ask.
#
# Paths are relative to the repo root.
set -euo pipefail

src="packages/watchers/skills/watchers"
dst="skills/watchers"
mkdir -p "$dst/scripts"
cp "$src/SKILL.md" "$dst/SKILL.md"
cp packages/watchers/scripts/_watermark.py \
  packages/watchers/scripts/watch_rss.py \
  packages/watchers/scripts/watch_http_json.py \
  packages/watchers/scripts/watch_github.py \
  "$dst/scripts/"

mkdir -p data/watcher-state

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply watchers --source bundled
