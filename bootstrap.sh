#!/usr/bin/env bash
# chief bootstrap — the curl|bash one-liner (#154).
#
# Takes a fresh Mac or Debian-family Linux machine to chatting with chief in the
# browser: installs the prerequisites (Homebrew-first on macOS, apt on Linux),
# clones the repo to a standard location at the latest tagged release, and hands
# off to install.sh (wizard, deps, launcher, autostart service, launch).
# Re-runs are idempotent: an existing clone is updated, never destroyed.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Wilcus-Industries/chief/main/bootstrap.sh | bash
#   curl -fsSL .../bootstrap.sh | bash -s -- [flags]
#
#   --dir DIR          install location (default: ~/.local/share/chief)
#   --ref TAG          install a specific tag instead of the latest release
#   --no-service       skip the autostart service
#   --no-launch        do not start the daemon / open the browser at the end
#   --non-interactive  no prompts (env: CHIEF_OWNER_PASSWORD,
#                      CHIEF_OPENROUTER_KEY, CHIEF_BUDGET_CAP)
#
# Model auth is required to chat: an OpenRouter API key
# (https://openrouter.ai/settings/keys). The wizard walks you through it.

set -euo pipefail

CHIEF_REPO_URL="${CHIEF_REPO_URL:-https://github.com/Wilcus-Industries/chief.git}"
# Parameterized so tests can point OS detection at a fixture file.
CHIEF_OS_RELEASE="${CHIEF_OS_RELEASE:-/etc/os-release}"

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
miss() { printf '  \033[33m✗\033[0m %s\n' "$*"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() { sed -n '2,23p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' || true; }

detect_os() {
  case "$(uname -s)" in
    Darwin) echo darwin ;;
    Linux)
      if [ -r "$CHIEF_OS_RELEASE" ] \
        && grep -Eq '^(ID="?(debian|ubuntu)"?$|ID_LIKE=.*debian)' \
          "$CHIEF_OS_RELEASE"; then
        echo debian
      else
        echo linux-other
      fi
      ;;
    *) echo unsupported ;;
  esac
}

APT_UPDATED=0
apt_run() {
  if [ "$(id -u)" = 0 ]; then
    apt-get "$@"
  else
    sudo apt-get "$@"
  fi
}
apt_install() {
  if [ "$APT_UPDATED" = 0 ]; then
    apt_run update -y
    APT_UPDATED=1
  fi
  apt_run install -y "$@"
}

ensure_brew() {
  if have brew; then
    ok "homebrew"
    return
  fi
  say "installing Homebrew"
  NONINTERACTIVE=1 /bin/bash -c \
    "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  local prefix
  for prefix in /opt/homebrew /usr/local; do
    if [ -x "$prefix/bin/brew" ]; then
      eval "$("$prefix/bin/brew" shellenv)"
      break
    fi
  done
  have brew || fail "Homebrew did not land on PATH — open a new terminal and re-run"
  ok "homebrew"
}

ensure_git() {
  local os="$1"
  if have git; then
    ok "git"
    return
  fi
  case "$os" in
    darwin) brew install git ;;
    debian) apt_install git ca-certificates ;;
    *) fail "git is required — install it and re-run (https://git-scm.com)" ;;
  esac
  ok "git"
}

ensure_uv() {
  local os="$1"
  if have uv; then
    ok "uv"
    return
  fi
  case "$os" in
    darwin) brew install uv ;;
    *)
      curl -LsSf https://astral.sh/uv/install.sh | sh
      export PATH="$HOME/.local/bin:$PATH"
      ;;
  esac
  have uv || fail "uv install failed (https://docs.astral.sh/uv/)"
  ok "uv"
}

# Clone (or update) the repo and pin it to a release tag. Never destroys local
# state: a dirty tree skips the checkout with a warning.
fetch_repo() {
  local dir="$1" ref="${2:-}"
  if [ -d "$dir/.git" ]; then
    say "existing install found at $dir — updating"
    git -C "$dir" fetch --tags --force origin
  else
    say "cloning chief to $dir"
    mkdir -p "$(dirname "$dir")"
    git clone "$CHIEF_REPO_URL" "$dir"
  fi
  local tag="$ref"
  if [ -z "$tag" ]; then
    tag="$(git -C "$dir" tag --sort=-v:refname | head -n1)"
  fi
  if [ -n "$tag" ]; then
    if [ -n "$(git -C "$dir" status --porcelain)" ]; then
      miss "local changes in $dir — leaving the current checkout as-is"
    else
      git -C "$dir" checkout --quiet "$tag"
      ok "at release $tag"
    fi
  else
    miss "no release tags yet — using the default branch tip"
  fi
}

main() {
  local install_flags="" ref=""
  local dir="${CHIEF_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/chief}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --dir) dir="$2"; shift ;;
      --ref) ref="$2"; shift ;;
      --no-service|--no-launch|--non-interactive)
        install_flags="$install_flags $1"
        ;;
      -h|--help) usage; exit 0 ;;
      *) fail "unknown flag: $1 (try --help)" ;;
    esac
    shift
  done

  say "chief — one-line install"
  echo "  to chat you will need an OpenRouter API key"
  echo "  (https://openrouter.ai/settings/keys). The wizard walks you through it."

  local os
  os="$(detect_os)"
  case "$os" in
    unsupported)
      fail "unsupported OS ($(uname -s)) — chief installs on macOS and Debian-family Linux"
      ;;
    linux-other)
      miss "non-Debian Linux — best effort: git and uv must already be installed"
      ;;
    darwin) ensure_brew ;;
  esac

  ensure_git "$os"
  ensure_uv "$os"
  fetch_repo "$dir" "$ref"

  say "handing off to install.sh"
  cd "$dir"
  # deliberate word-splitting of the flag string
  # shellcheck disable=SC2086
  exec bash ./install.sh $install_flags
}

# Run main unless this file is being sourced (tests source it to reach the
# functions). Under `curl | bash` BASH_SOURCE is empty, so main runs there too.
if [ -z "${BASH_SOURCE[0]:-}" ] || [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
