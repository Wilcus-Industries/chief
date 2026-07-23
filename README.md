# Chief

A personal agent that runs on your own machine and can edit its own code.

I built Chief for myself. It's a small core (the agent loop, sessions, an event
bus, monitors, a tool gate, budget tracking, an audit log) plus packages for
everything else. You run it on a Linux box or a Mac, chat with it in the
browser, and add capabilities by asking. When it needs something it doesn't
have, it installs a package, and sometimes writes the code for one.

It's single-owner: your machine, your keys, your data. There's no hosted version
and no multi-user mode.

Status: alpha. I work on it actively, so things move around. The design is
written up in [DESIGN.md](./DESIGN.md).

## How it's put together

The core stays small on purpose. Channels, Google, a browser, memory: all of it
lives in packages rather than the core. A package can be simple (Chief runs a
setup script and restarts) or involved (it configures an integration, walks an
OAuth flow, or writes a new channel adapter into Chief's own source).

Chief can edit its own config, prompts, skills, and Python. Edits to its source
follow a fixed path: make a branch, run the tests/lint/types, restart into the
new code, health-check, roll back if that fails. There's no human review step.
Every change lands in an audit log instead.

The userspace is left open, roughly in the spirit of Arch Linux. You can give
Chief permanent approval to edit and restart itself. You can meaningfully alter
and customize any package you install. While there are certainly best practices,
Chief is yours to do with what you want.

## What it does

- Web UI at `http://127.0.0.1:8130` for chat, approvals, and the monitor list.
  Password protected.
- iMessage on macOS, if you set it up. A package decides which handles and
  threads can wake it.
- Adds capabilities on request. Ask it to set up Google, add a channel, or
  install a memory system, and it runs the package.
- Monitors and schedules. Put a monitor on a channel (a regex, a yes/no
  judgment, or a classifier) and it wakes on matching events. Cron and interval
  schedules cover recurring work.
- A monthly spend cap, set at install, with warning and cutoff thresholds.
- An append-only audit log of every tool call and approval.

## Install

macOS or Debian-family Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/Wilcus-Industries/chief/main/bootstrap.sh | bash
```

Read the script before you run it. The URL above is the whole thing.

You need an OpenRouter API key to chat
(<https://openrouter.ai/settings/keys>). The first-run wizard asks for it, plus
a web password and a monthly spend cap.

The one-liner installs prerequisites (Homebrew on macOS, apt on Linux), clones
Chief to `~/.local/share/chief` at the latest tagged release, runs the wizard,
installs the `chief` command and an autostart service (launchd or systemd user
unit, skip with `--no-service`), starts the daemon, and opens the web UI.
Running it again updates an existing install instead of wiping it.

From a clone, `./install.sh` does the same without the prerequisite step.

## Running it

```
chief status     # service + web UI state
chief update     # merge the latest origin/main, restart, roll back if unhealthy
chief stop       # stop the daemon (chief start brings it back)
chief run        # run in the foreground instead of the service
chief wizard     # re-run the first-run wizard
chief uninstall  # remove service + launcher; --purge-data removes data too
```

The first conversation is the setup. Chief introduces itself and offers
packages; everything past the core gets installed by asking for it in chat.
There's also a local socket client: `uv run chief-cli`.

## How a message flows

Every inbound message (web, iMessage, socket) becomes a `Message(channel,
sender, thread_key, text)` on the event bus and hits one turn loop. The reply
goes back out the channel it came in on. Each thread gets its own session and a
serial queue; threads run concurrently.

Chief uses its own agent loop over a provider seam rather than a vendor SDK.
OpenRouter is the provider it ships with. The default tool set is small
(`read_file`, `grep`, `write_file`, `edit_file`, `shell`, `restart`, `session`,
`switch_model`, `monitor`, `schedule`, `load_skill`, `spawn_agent`); packages
register anything else, and package install itself is document-driven through
`shell` rather than a dedicated tool.

Source edits run the done-check before they take effect:
`uv run pytest && uv run ruff check . && uv run mypy .`. Files are capped at 200
lines so the core stays small enough to read.

More detail is in [DESIGN.md](./DESIGN.md) and [docs/](./docs) (ARCHITECTURE,
LIFECYCLE, SUBSYSTEMS, SECURITY, CONFIG, OPERATIONS, EXTENDING, TESTING).
Contributions follow [CONTRIBUTING.md](./CONTRIBUTING.md) and
[STYLEGUIDE.md](./STYLEGUIDE.md), with the same done-check.
Releases are git tags.

## Security and privacy

Chief runs as you, holds your keys, and reads whatever you connect it to.

- Single-owner. Unknown senders are logged (metadata only) and never wake the
  agent or get a reply.
- Secrets live in `secrets/` (one per file, `chmod 0600`) or env vars, never in
  tracked files.
- The tool gate is code and fails closed: never/approve lists, with an approval
  card on the surface you're chatting from for anything in between. How Chief
  behaves is prompt and policy; what it's allowed to run is code.
- The bundled screening package runs a cheap-model check for prompt injection on
  untrusted content, and every public-facing package depends on it. It's a
  baseline; adjust it to your own threat model.
- Run Chief on a capable model. It's autonomous and edits itself, so a weak
  model will make messes. Be careful with standing `always` approvals. If you
  want the approval flow to work differently, ask Chief to change it.

## License

MIT, see [LICENSE](./LICENSE).
