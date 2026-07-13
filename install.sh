#!/usr/bin/env bash
# chief installer — host-native.
#
# Core runs natively on this machine; only the MCP sidecars (Google servers,
# playwright) live in docker compose. This script checks prerequisites, scaffolds
# the config/secrets/data layout, installs Python deps, runs the DB migrations,
# walks the first-run wizard (owner password + model auth — no platform bot
# tokens; the web UI is the day-one channel), installs the `chief` launcher and
# the autostart service, then starts the daemon, waits for health, and opens the
# web UI. Normally invoked by bootstrap.sh (the curl|bash one-liner); running it
# from a clone works too. Re-runs are idempotent — existing secrets, data, and
# services are kept. macOS (bash 3.2) and Linux compatible.
#
# Usage:
#   ./install.sh [--google] [--playwright] [--no-service] [--no-launch] [--non-interactive]
#
#   --google           build + start the Google MCP sidecars (compose profile "google")
#   --playwright       build + start the browser sidecar (compose profile "playwright")
#   --no-service       skip the autostart service (launchd agent / systemd user unit)
#   --no-launch        do not start the daemon or open the browser at the end
#   --non-interactive  no wizard prompts (env: CHIEF_OWNER_PASSWORD, CHIEF_OPENROUTER_KEY)

set -euo pipefail

REPO_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$REPO_DIR"

WITH_GOOGLE=0
WITH_PLAYWRIGHT=0
NO_SERVICE=0
NO_LAUNCH=0
NON_INTERACTIVE=0
for arg in "$@"; do
  case "$arg" in
    --google) WITH_GOOGLE=1 ;;
    --playwright) WITH_PLAYWRIGHT=1 ;;
    --no-service) NO_SERVICE=1 ;;
    --no-launch) NO_LAUNCH=1 ;;
    --non-interactive) NON_INTERACTIVE=1 ;;
    -h|--help)
      sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown flag: $arg (try --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
miss() { printf '  \033[33m✗\033[0m %s\n' "$*"; }

fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- prerequisites -----------------------------------------------------------
say "checking prerequisites"
command -v git >/dev/null 2>&1 \
  || fail "git is required — bootstrap.sh (the curl one-liner) auto-installs it (https://git-scm.com)"
ok "git"
command -v uv >/dev/null 2>&1 \
  || fail "uv is required — bootstrap.sh (the curl one-liner) auto-installs it (https://docs.astral.sh/uv/)"
ok "uv"
# Docker powers the MCP sidecars. Its absence only blocks those, so it fails the
# install only when a sidecar profile was explicitly requested (PRD #154: guide,
# don't abort — bootstrap.sh does the guiding).
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ok "docker + compose"
else
  if [ "$WITH_GOOGLE" = 1 ] || [ "$WITH_PLAYWRIGHT" = 1 ]; then
    fail "docker + compose v2 are required for the requested sidecars (https://docs.docker.com)"
  fi
  miss "docker not found — MCP sidecars stay unavailable until it is installed (https://docs.docker.com)"
fi

# ---- repo layout -------------------------------------------------------------
say "scaffolding directories"
git submodule update --init --recursive
mkdir -p data/screenshots data/workspace secrets/google_tokens
ok "data/ (sqlite, audit log, memory, workspace, screenshots — gitignored)"
ok "secrets/ (one file per secret — gitignored)"

# ---- python deps + migrations --------------------------------------------------
say "installing python dependencies (uv sync)"
uv sync

say "running database migrations"
uv run python -m chief.install migrate

# ---- first-run wizard -----------------------------------------------------------
# Replaces the old manual secrets checklist: owner password (the web UI
# credential, hashed into the secrets dir) + model auth (Copilot login or
# OpenRouter key, validated). No platform bot token is requested — the web UI is
# the day-one channel; Telegram/Discord connect later in the web Settings pages.
if [ "$NON_INTERACTIVE" = 1 ]; then
  say "first-run wizard (non-interactive)"
  uv run python -m chief.install wizard --non-interactive
elif [ -r /dev/tty ]; then
  say "first-run wizard"
  uv run python -m chief.install wizard < /dev/tty
else
  say "first-run wizard (no terminal — non-interactive)"
  uv run python -m chief.install wizard --non-interactive
fi

# ---- MCP sidecars ---------------------------------------------------------------
# (plain string, not an array: empty-array expansion trips set -u on macOS bash 3.2)
PROFILE_FLAGS=""
if [ "$WITH_GOOGLE" = 1 ]; then
  PROFILE_FLAGS="$PROFILE_FLAGS --profile google"
fi
if [ "$WITH_PLAYWRIGHT" = 1 ]; then
  PROFILE_FLAGS="$PROFILE_FLAGS --profile playwright"
fi
if [ -n "$PROFILE_FLAGS" ]; then
  say "building + starting MCP sidecars ($PROFILE_FLAGS)"
  # deliberate word-splitting of the flag string
  # shellcheck disable=SC2086
  docker compose $PROFILE_FLAGS up -d --build
else
  say "skipping MCP sidecars (pass --google / --playwright to start them)"
fi

# ---- launcher -------------------------------------------------------------------
say "installing the 'chief' launcher"
BIN_DIR="${HOME}/.local/bin"
mkdir -p "$BIN_DIR"
UV_BIN="$(command -v uv)"
cat > "$BIN_DIR/chief" <<EOF
#!/usr/bin/env bash
# chief launcher (generated by install.sh) — dispatches to the repo's tooling.
set -euo pipefail
REPO_DIR="$REPO_DIR"
UV_BIN="$UV_BIN"
if [ ! -x "\$UV_BIN" ]; then
  UV_BIN="\$(command -v uv)" || { echo "uv not found" >&2; exit 1; }
fi
cd "\$REPO_DIR"
CMD="\${1:-help}"
case "\$CMD" in
  run)
    exec "\$UV_BIN" run python -m chief.entrypoint
    ;;
  start|stop|status|update|wizard|uninstall)
    shift
    exec "\$UV_BIN" run python -m chief.install "\$CMD" "\$@"
    ;;
  help|-h|--help)
    cat <<'USAGE'
chief — personal AI agent
  chief run        run the daemon in the foreground
  chief start      start the daemon (autostart service)
  chief stop       stop the daemon
  chief status     service + web UI state
  chief update     jump to the newest tagged release (migrations + restart)
  chief wizard     re-run the first-run wizard (password / model auth)
  chief uninstall  remove service + launcher (--purge-data removes data too)
USAGE
    ;;
  *)
    echo "unknown command: \$CMD (try: chief help)" >&2
    exit 2
    ;;
esac
EOF
chmod +x "$BIN_DIR/chief"
ok "$BIN_DIR/chief"
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) miss "$BIN_DIR is not on your PATH — add it, or run: uv run python -m chief.entrypoint" ;;
esac

# ---- autostart service ------------------------------------------------------------
if [ "$NO_SERVICE" = 1 ]; then
  say "skipping the autostart service (--no-service)"
else
  say "installing the autostart service (launchd agent / systemd user unit)"
  uv run python -m chief.install service-install \
    --repo "$REPO_DIR" --launcher "$BIN_DIR/chief"
fi

# ---- launch -----------------------------------------------------------------------
if [ "$NO_LAUNCH" = 1 ]; then
  say "skipping launch (--no-launch)"
  echo "  start chief with: chief start (service) or chief run (foreground)"
else
  if [ "$NO_SERVICE" = 1 ]; then
    say "starting the daemon in the background (no service installed)"
    nohup "$BIN_DIR/chief" run >> data/chief.log 2>&1 &
  fi
  say "waiting for the web UI"
  if uv run python -m chief.install await-health --timeout 120; then
    uv run python -m chief.install open-browser
  else
    miss "the web UI did not come up — check data/chief.log (or: journalctl --user -u chief)"
  fi
fi

# ---- summary ----------------------------------------------------------------------
say "done"
echo "  chat in the browser — the web UI is chief's day-one channel; no platform"
echo "  bot token is needed. Connect Telegram/Discord later in the web Settings."
echo "  lifecycle: chief start | stop | status | update | uninstall"
echo "  config: config.yaml (owner ids, enabled services, blacklist, screening)"
echo "  google: mint the shared token once with 'uv run python -m chief.tools.google.auth'"
