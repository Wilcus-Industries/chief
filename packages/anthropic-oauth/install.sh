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
# The done-check gates the restart, but config.yaml is gitignored — a bad
# config write is NOT rolled back (the pre-restart config gate is the only
# protection). Paths are
# relative to the repo root.
set -euo pipefail

: "${PROXY_URL:?set PROXY_URL to the proxy base, e.g. http://127.0.0.1:8000/v1}"
: "${MODEL:?set MODEL to the bare model id the proxy exposes}"
alias_name="${ALIAS:-opus}"

# An http(s) scheme is required: it keeps a leading-dash value from being
# parsed as curl options and rejects obviously broken bases before any probe.
case "$PROXY_URL" in
  http://*|https://*) ;;
  *)
    echo "error: PROXY_URL must start with http:// or https://" \
      "(got: $PROXY_URL) — nothing was changed" >&2
    exit 1
    ;;
esac

# Fail fast BEFORE any mutation: wiring a dead endpoint or a missing secret
# into config.yaml would route model calls at a black hole while the install
# "succeeds". Nothing below runs until the proxy actually answers.
if [ ! -s secrets/proxy_api_key ]; then
  echo "error: secrets/proxy_api_key is missing or empty — write the proxy's" \
    "bearer token there first (see INSTALL.md), then re-run" >&2
  exit 1
fi
if ! curl -fsS --max-time 5 \
  -H "Authorization: Bearer $(cat secrets/proxy_api_key)" \
  --url "${PROXY_URL%/}/models" >/dev/null; then
  echo "error: proxy at $PROXY_URL did not answer GET /models — start the" \
    "proxy first, then re-run (nothing was changed)" >&2
  exit 1
fi

src="packages/anthropic-oauth/skills/anthropic-oauth"
dst="skills/anthropic-oauth"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

uv run python -m chief.config_apply \
  "provider_backends.proxy={base_url: '$PROXY_URL', api_key_secret: proxy_api_key}" \
  "provider_aliases.$alias_name={backend: proxy, model: '$MODEL'}"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply anthropic-oauth --source bundled
