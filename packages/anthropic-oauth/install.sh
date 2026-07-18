#!/usr/bin/env bash
# Deterministic install for anthropic-oauth: copy the skill verbatim and point
# the provider's base_url at a local OpenAI-compatible proxy plus set the model.
# The interactive parts (running the proxy, setting the CCI_API_KEY secret,
# verifying a turn) are NOT here — the agent does them from INSTALL.md.
#
# Parameters (env):
#   PROXY_URL  the proxy's OpenAI-compatible base, e.g. http://127.0.0.1:8000/v1
#   MODEL      the bare model name the proxy exposes, e.g. claude-sonnet-4.5
#
# Runs inside the self-edit seatbelt (done-check + rollback); paths are
# relative to the repo root.
set -euo pipefail

src="packages/anthropic-oauth/skills/anthropic-oauth"
dst="skills/anthropic-oauth"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

: "${PROXY_URL:?set PROXY_URL to the proxy base, e.g. http://127.0.0.1:8000/v1}"
: "${MODEL:?set MODEL to the bare model name the proxy exposes}"

uv run python -m chief.config_apply \
  "provider_base_url=$PROXY_URL" \
  "models.default=$MODEL"
