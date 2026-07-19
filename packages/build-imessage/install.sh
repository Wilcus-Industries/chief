#!/usr/bin/env bash
# Deterministic install for build-imessage: install the imsg CLI, copy the
# skills verbatim, and set the standard config keys. The customizable +
# interactive parts (which setup mode, Full Disk Access, the Automation
# prompt, the notify-tier monitor) are NOT here — the agent does them from
# INSTALL.md.
#
# Parameters (env):
#   IMESSAGE_HANDLES  comma-separated owner handle(s), e.g. "+15551234567"
#                     (the self-chat id for self-DM mode, or the owner's phone
#                     for a dedicated Apple ID) — see INSTALL.md step 1.
#
# Runs inside the self-edit seatbelt (done-check + rollback); paths are
# relative to the repo root.
set -euo pipefail

# imsg — the CLI chief drives (via the shell tool) to send outbound iMessages
# to any recipient and read/search conversations on demand; the core adapter
# only polls inbound + auto-replies to the owner's self-chat. Idempotent:
# skip if already on PATH. Requires Homebrew (macOS-only package).
if ! command -v imsg >/dev/null 2>&1; then
  brew install steipete/tap/imsg
fi

# Copy both skills verbatim: build-imessage (channel policy) + imsg (CLI usage).
for skill in build-imessage imsg; do
  src="packages/build-imessage/skills/$skill"
  dst="skills/$skill"
  mkdir -p "$dst"
  cp "$src/SKILL.md" "$dst/SKILL.md"
done

: "${IMESSAGE_HANDLES:?set IMESSAGE_HANDLES to the owner handle(s)}"

# Build a YAML list from the comma-separated handles: +1,+2 -> ["+1","+2"]
handles_yaml="["
IFS=',' read -ra parts <<<"$IMESSAGE_HANDLES"
for handle in "${parts[@]}"; do
  handle="${handle//[[:space:]]/}"
  [ -n "$handle" ] && handles_yaml+="\"$handle\","
done
handles_yaml+="]"

uv run python -m chief.config_apply \
  imessage.enabled=true \
  "imessage.owner_handles=$handles_yaml"
