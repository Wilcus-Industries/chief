# CLAUDE.md

> **Read first:** [`STYLEGUIDE.md`](./STYLEGUIDE.md) — code conventions, follow on every change.
>
> **Plan / roadmap:** [`DESIGN.md`](./DESIGN.md) — target architecture for the greenfield
> rewrite (minimal core, packages, self-edit). Read it before starting work.

Project-specific rules only. The universal working rules — boundaries, when-stuck,
secrets, done-honesty — live in the global `~/.claude/CLAUDE.md` and apply underneath
this. Only add a rule here when it differs from, or isn't covered by, the global.

## Run story (host-native)

Core runs natively on this machine — no containers, no migrations (the schema is
created at boot). Fresh-machine setup is the one-liner (`bootstrap.sh` →
`install.sh`, README.md); from a clone, `./install.sh` does prereqs, scaffold,
deps, the first-run wizard (web password, OpenRouter key, monthly budget cap),
the `chief` launcher, and the autostart service (`--no-service` / `--no-launch` /
`--non-interactive` for automation; re-runs are idempotent). Run in the foreground
with `chief run` or `uv run python -m chief.entrypoint`; lifecycle via
`chief start|stop|status|update|uninstall` — `update` merges `origin/main`
(never a checkout: self-edit means every install carries local commits),
re-syncs installed skill copies, restarts, and rolls back if the daemon comes
back unhealthy. Attach the plain-REPL socket client with `uv run chief-cli`
(`--socket` overrides `socket_path`); it is only a client, so quitting it leaves
the daemon running. The web UI serves in-process at `http://127.0.0.1:8130`
(`web_*` config keys) — owner-password chat + approvals + monitors, server-
rendered, fail-closed without a password. Capabilities beyond core (channels,
Google, memory) are packages the agent installs in chat (`packages/` bundled,
more from the chief-packages repo).

## Packages live in chief-packages, not here

`packages/` is for the few packages that **must** ship atomically with core —
ones whose skills document core internals, so a split repo would let the two
skew (today: `screening`, `build-imessage`, `anthropic-oauth`). Everything else
belongs in the chief-packages repo.

**Adding a new package to this repo needs the owner's explicit approval.** Ask,
and say why it can't live in chief-packages. Default answer is chief-packages.

## Definition of done

The project's full check — the global done-rule points here for the exact commands. All
must pass before any task is "done":

```
uv run pytest
uv run ruff check .
uv run mypy .
```

## Keep these docs current

Treat `CLAUDE.md` and `STYLEGUIDE.md` as living docs, not write-once boilerplate. As part
of a change that makes a rule here stale, wrong, or redundant, prune or rewrite it in the
same change; add a rule when a real, recurring need shows up. Keep it tight — fewer,
sharper lines beat an accreting pile. (This project directive overrides the global
ask-first-before-editing-docs rule for these two files.)
