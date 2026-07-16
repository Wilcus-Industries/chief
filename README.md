# chief

A personal AI agent that runs natively on your own machine: a minimal core
(agent loop, sessions, events, gate, budget) plus installable packages for
everything else. Chat with it in the browser from minute one; ask it to set up
iMessage (macOS), Google, memory, or new channels — it installs packages and
edits its own code through a guarded pipeline. Design and internals:
[`DESIGN.md`](./DESIGN.md).

## Install (macOS or Debian-family Linux)

```sh
curl -fsSL https://raw.githubusercontent.com/CrazyWillBear/chief/main/bootstrap.sh | bash
```

Piping a script to your shell deserves a look first — the URL above is the whole
script; read it before running it.

**You will need an OpenRouter API key to chat**
(<https://openrouter.ai/settings/keys>) — the wizard walks you through it,
along with the web UI password and a monthly spend cap.

The one-liner installs the prerequisites (Homebrew-first on macOS, apt on
Linux), clones chief to `~/.local/share/chief` at the latest tagged release,
runs the short terminal wizard, installs the `chief` command and an autostart
service (launchd agent / systemd user unit — skip with `--no-service`), starts
the daemon, and opens the web UI. Re-running it is safe: an existing install
is updated, never wiped.

From a clone, `./install.sh` does the same without the prerequisite bootstrap.

## After install

```
chief status     # service + web UI state
chief update     # jump to the newest tagged release (restart included)
chief stop       # stop the daemon (chief start brings it back)
chief run        # run the daemon in the foreground instead of the service
chief wizard     # re-run the first-run wizard (password / key / budget cap)
chief uninstall  # remove service + launcher; --purge-data removes data too
```

The web UI lives at `http://127.0.0.1:8130`. The first conversation is
onboarding: the agent introduces itself and offers packages. Everything beyond
core — channels, Google tools, memory systems — is installed by asking for it
in chat (see `packages/` for the bundled defaults and
[`secrets/README.md`](./secrets/README.md) for the secrets layout). A local
control socket is always on too: `uv run chief-cli`.

## Developing

Working rules live in [`CLAUDE.md`](./CLAUDE.md) and [`STYLEGUIDE.md`](./STYLEGUIDE.md).
The done-check is `uv run pytest && uv run ruff check . && uv run mypy .`.
Releases are git tags — `bootstrap.sh` installs, and `chief update` updates
to, the newest tag.
