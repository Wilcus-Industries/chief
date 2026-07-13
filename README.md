# chief

A personal AI agent that runs natively on your own machine. Chat with it in the
browser from minute one; connect Telegram, Discord, and the Google/browser tool
sidecars later from its settings pages. Design and internals: [`DESIGN.md`](./DESIGN.md).

## Install (macOS or Debian-family Linux)

```sh
curl -fsSL https://raw.githubusercontent.com/CrazyWillBear/chief/main/bootstrap.sh | bash
```

Piping a script to your shell deserves a look first — the URL above is the whole
script; read it before running it.

**You will need model auth to chat** (the wizard walks you through either path):

- a **GitHub Copilot** subscription — the free tier works, with limits — via the
  Copilot CLI login, or
- an **OpenRouter API key** (<https://openrouter.ai/settings/keys>).

The one-liner installs the prerequisites (Homebrew-first on macOS, apt on Linux;
Docker is guided, not assumed), clones chief to `~/.local/share/chief` at the
latest tagged release, runs a short terminal wizard (owner password → the web UI
login, plus model auth), installs the `chief` command and an autostart service
(launchd agent / systemd user unit — skip with `--no-service`), starts the
daemon, and opens the web UI. No config files to edit, no bot accounts to create.
Re-running it is safe: an existing install is updated, never wiped.

From a clone, `./install.sh` does the same without the prerequisite bootstrap
(`--google` / `--playwright` also start the MCP sidecars).

## After install

```
chief status     # service + web UI state
chief update     # jump to the newest tagged release (migrations + restart)
chief stop       # stop the daemon (chief start brings it back)
chief run        # run the daemon in the foreground instead of the service
chief wizard     # re-run the first-run wizard (password / model auth)
chief uninstall  # remove service + launcher; --purge-data removes data too
```

The web UI lives at `http://127.0.0.1:8130` (LAN exposure is an opt-in setting).
Platform chats (Telegram / Discord), Google sidecars, and the rest are connected
from the web UI's Settings and documented in [`secrets/README.md`](./secrets/README.md).

## Developing

Working rules live in [`CLAUDE.md`](./CLAUDE.md) and [`STYLEGUIDE.md`](./STYLEGUIDE.md).
The done-check is `uv run pytest && uv run ruff check . && uv run mypy .`; CI also
shellchecks the installer scripts, and the opt-in `installer-e2e` workflow runs the
real one-liner on a clean Debian container. Releases are git tags — `bootstrap.sh`
installs, and `chief update` updates to, the newest tag.
