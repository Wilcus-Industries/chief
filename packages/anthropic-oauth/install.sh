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
#   MODEL      a bare model id the proxy exposes, e.g. claude-opus-4-8
#   ALIAS      the typed name to route with
#
# With neither MODEL nor ALIAS set, the three standard aliases are installed at
# once — the common case, so `/model opus|sonnet|haiku` works straight after
# install. Pass BOTH MODEL and ALIAS to install exactly one instead (a model
# the defaults don't cover). Either way config deep-merges, so re-running adds
# aliases without clobbering existing ones.
#
# Each alias is checked against the proxy's own /models list before anything is
# written: a model id the proxy doesn't serve would otherwise be accepted here
# and only surface later as a failing turn, with the thread already pinned to
# it.
#
# The done-check gates the restart, but config.yaml is gitignored — a bad
# config write is NOT rolled back (the pre-restart config gate is the only
# protection). Paths are
# relative to the repo root.
set -euo pipefail

: "${PROXY_URL:?set PROXY_URL to the proxy base, e.g. http://127.0.0.1:8000/v1}"

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
if ! served=$(curl -fsS --max-time 5 \
  -H "Authorization: Bearer $(cat secrets/proxy_api_key)" \
  --url "${PROXY_URL%/}/models"); then
  echo "error: proxy at $PROXY_URL did not answer GET /models — start the" \
    "proxy first, then re-run (nothing was changed)" >&2
  exit 1
fi

# Argument shape is resolved only after the fail-fast checks above, so a dead
# proxy or missing secret is still reported first — those are the errors worth
# seeing. The three standard aliases, or exactly the one asked for: requiring
# BOTH MODEL and ALIAS together keeps a half-specified run from silently
# installing something other than what was meant.
if [ -z "${MODEL:-}" ] && [ -z "${ALIAS:-}" ]; then
  pairs="opus=claude-opus-4-8
sonnet=claude-sonnet-4-6
haiku=claude-haiku-4-5-20251001"
elif [ -n "${MODEL:-}" ] && [ -n "${ALIAS:-}" ]; then
  pairs="$ALIAS=$MODEL"
else
  echo "error: set BOTH MODEL and ALIAS to install a single alias, or" \
    "NEITHER to install the standard opus/sonnet/haiku set" >&2
  exit 1
fi

# Reject an id this proxy does not serve, while it is still cheap to say so.
# Some proxies echo an unknown id back on a completion instead of erroring, so
# the /models list — not a test turn — is the honest check.
served_ids=$(printf '%s' "$served" | uv run python -c \
  'import json,sys; print("\n".join(m["id"] for m in json.load(sys.stdin)["data"]))')
while IFS='=' read -r alias_name model_id; do
  if ! printf '%s\n' "$served_ids" | grep -Fxq "$model_id"; then
    echo "error: proxy at $PROXY_URL does not serve '$model_id' (alias" \
      "'$alias_name') — nothing was changed. It serves:" >&2
    printf '%s\n' "$served_ids" | sed 's/^/  /' >&2
    exit 1
  fi
done <<EOF
$pairs
EOF

src="packages/anthropic-oauth/skills/anthropic-oauth"
dst="skills/anthropic-oauth"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# One config_apply call: every alias lands together or not at all, so a failure
# partway through can't leave half the set wired up.
apply_args=(
  "provider_backends.proxy={base_url: '$PROXY_URL', api_key_secret: proxy_api_key}"
)
while IFS='=' read -r alias_name model_id; do
  apply_args+=("provider_aliases.$alias_name={backend: proxy, model: '$model_id'}")
done <<EOF
$pairs
EOF
uv run python -m chief.config_apply "${apply_args[@]}"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply anthropic-oauth --source bundled
