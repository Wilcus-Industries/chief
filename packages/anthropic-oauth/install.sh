#!/usr/bin/env bash
# Deterministic install for anthropic-oauth: copy the skill verbatim and add a
# named "proxy" backend plus a typed alias, so `/model <alias>` (or the agent's
# switch_model) routes THAT thread onto a local Claude-subscription proxy while
# every other model stays on the default backend (OpenRouter). The interactive
# parts (running the proxy, writing secrets/proxy_api_key, verifying a turn) are
# NOT here — the agent does them from INSTALL.md.
#
# Parameters (env):
#   PROXY_URL  the proxy's OpenAI-compatible base, e.g. http://127.0.0.1:8000/v1
#   MODEL      the bare model id the proxy exposes, e.g. claude-opus-4-8
#   ALIAS      the typed name to route with (default: opus)
#
# Re-run with a different ALIAS/MODEL to add more aliases — config deep-merges,
# so each run adds one alias without clobbering the others.
#
# Runs inside the self-edit seatbelt (done-check + rollback); paths are
# relative to the repo root.
set -euo pipefail

src="packages/anthropic-oauth/skills/anthropic-oauth"
dst="skills/anthropic-oauth"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

: "${PROXY_URL:?set PROXY_URL to the proxy base, e.g. http://127.0.0.1:8000/v1}"
: "${MODEL:?set MODEL to the bare model id the proxy exposes}"
alias_name="${ALIAS:-opus}"

uv run python -m chief.config_apply \
  "provider_backends.proxy={base_url: $PROXY_URL, api_key_secret: proxy_api_key}" \
  "provider_aliases.$alias_name={backend: proxy, model: $MODEL}"
