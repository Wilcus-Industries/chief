#!/usr/bin/env bash
# chief installer — host-native.
#
# Core runs natively on this machine. This script checks prerequisites,
# scaffolds the config/secrets/data layout, installs Python deps, walks the
# first-run wizard (owner password → the web UI login, OpenRouter API key,
# monthly budget cap), offers chief its own system account, installs the
# `chief` launcher and the autostart service, then starts the daemon, waits
# for health, and opens the web UI. A dedicated-account install ends WITHOUT
# starting the daemon — chief has no graphical session until its first login.
# There are no migrations — the daemon creates its schema at boot. Everything
# beyond core (channels, Google, memory, …) installs later as packages, from
# inside the chat. Normally invoked by bootstrap.sh (the curl|bash one-liner);
# running it from a clone works too. Re-runs are idempotent — existing
# secrets, data, and services are kept. macOS (bash 3.2) and Linux compatible.
#
# Usage:
#   ./install.sh [--no-service] [--no-launch] [--non-interactive] [--single-user]
#
#   --no-service       skip the autostart service (launchd agent / systemd user unit)
#   --no-launch        do not start the daemon or open the browser at the end
#   --non-interactive  no wizard prompts (env: CHIEF_OWNER_PASSWORD,
#                      CHIEF_OPENROUTER_KEY, CHIEF_BUDGET_CAP). Never creates a
#                      system account — chief runs as you.
#   --single-user      skip the dedicated-account offer outright

set -euo pipefail

REPO_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$REPO_DIR"

NO_SERVICE=0
NO_LAUNCH=0
NON_INTERACTIVE=0
SINGLE_USER=0
for arg in "$@"; do
  case "$arg" in
    --no-service) NO_SERVICE=1 ;;
    --no-launch) NO_LAUNCH=1 ;;
    --non-interactive) NON_INTERACTIVE=1 ;;
    --single-user) SINGLE_USER=1 ;;
    -h|--help)
      sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
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

# ---- repo layout -------------------------------------------------------------
say "scaffolding directories"
mkdir -p data secrets
ok "data/ (sqlite, audit log, cloned packages — gitignored)"
ok "secrets/ (one file per secret — gitignored)"

# ---- python deps ---------------------------------------------------------------
say "installing python dependencies (uv sync)"
uv sync

# ---- first-run wizard -----------------------------------------------------------
# Owner password (the web UI credential), OpenRouter API key (validated), and
# the monthly budget cap. No platform bot tokens — the web UI is the day-one
# channel; other channels are built later by packages, from inside the chat.
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

# ---- dedicated account -----------------------------------------------------------
# After the wizard on purpose: the account plan chowns the tree and locks down
# secrets/, and the wizard is what writes the files in there.
CHIEF_USER=""
CHIEF_HOME=""
ACCOUNT_REPORT="$REPO_DIR/data/account-setup"
if [ "$SINGLE_USER" = 1 ]; then
  say "skipping the dedicated-account offer (--single-user)"
elif [ "$NON_INTERACTIVE" = 1 ] || [ ! -r /dev/tty ]; then
  say "dedicated account: not offered (no terminal) — chief runs as you"
else
  say "dedicated system account"
  # Written aside and moved over the real report only on an account answer.
  # Re-runs are idempotent and the offer can be skipped (--single-user, no
  # tty) or declined, and none of those mean the account an earlier run set up
  # has gone away — but `chief uninstall` reads this file to decide whether
  # there is an account to remove at all, so erasing it strands one.
  FRESH_REPORT=$(mktemp)
  uv run python -m chief.install account \
    --tree "$REPO_DIR" --report "$FRESH_REPORT" < /dev/tty
  # -E, not BRE alternation: BSD grep (macOS — the platform this targets) does
  # not understand \(a\|b\), and a silent no-match installs the wrong mode.
  if grep -qE '^mode=(create|existing)$' "$FRESH_REPORT" 2>/dev/null; then
    mv "$FRESH_REPORT" "$ACCOUNT_REPORT"
    CHIEF_USER=$(sed -n 's/^user=//p' "$ACCOUNT_REPORT")
    CHIEF_HOME=$(sed -n 's/^home=//p' "$ACCOUNT_REPORT")
  else
    rm -f "$FRESH_REPORT"
  fi
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
  start|stop|status|update|check-updates|release|wizard|uninstall|compact|account)
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
  chief update     apply the newest release onto this box's own edits
                   (leaves the result staged; chief restarts to commit it)
  chief check-updates  is a newer core release out? (no changes)
  chief release <major|minor|patch>  cut a release (upstream repo only)
  chief wizard     re-run the first-run wizard (password / key / budget cap)
  chief account    give chief its own system user (interactive only)
  chief compact <thread>  force-compact a thread's history now (via the daemon)
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
elif [ -n "$CHIEF_USER" ]; then
  # A launchd agent cannot be bootstrapped into a session that does not exist
  # yet: write the definition into chief's home and let its first login load it.
  say "writing the autostart service into $CHIEF_USER's account (not started)"
  # The tree and the launcher both sit under the owner's home, which chief has
  # to traverse. macOS homes are 0755; distros that honour HOME_MODE=0700 are
  # not, and the service would fail at first login with an opaque exec error.
  sudo -u "$CHIEF_USER" test -r "$REPO_DIR/pyproject.toml" \
    || fail "$CHIEF_USER cannot read $REPO_DIR — grant it traversal (chmod o+x on the parents) or move the tree, then re-run"
  sudo -u "$CHIEF_USER" test -x "$BIN_DIR/chief" \
    || fail "$CHIEF_USER cannot run $BIN_DIR/chief — grant traversal or move the launcher somewhere shared, then re-run"
  # Written BY chief: the definition lands in chief's home, which the owner
  # cannot write (macOS ~/Library is 0700), and a chown afterwards is too late.
  sudo -u "$CHIEF_USER" -H "$UV_BIN" run python -m chief.install service-install \
    --repo "$REPO_DIR" --launcher "$BIN_DIR/chief" \
    --home "$CHIEF_HOME" --uid "$(id -u "$CHIEF_USER")" --no-start
else
  say "installing the autostart service (launchd agent / systemd user unit)"
  uv run python -m chief.install service-install \
    --repo "$REPO_DIR" --launcher "$BIN_DIR/chief"
fi

# ---- launch -----------------------------------------------------------------------
if [ -n "$CHIEF_USER" ]; then
  say "not starting the daemon — $CHIEF_USER has no login session yet"
  echo "  remaining steps were printed above; chief starts at ${CHIEF_USER}'s first login."
elif [ "$NO_LAUNCH" = 1 ]; then
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
echo "  chat in the browser — the first conversation is onboarding: chief"
echo "  introduces itself and offers packages (iMessage on macOS, Google, memory)."
echo "  lifecycle: chief start | stop | status | update | uninstall"
echo "  config: config.yaml (models, gate lists, budget, quiet hours)"
