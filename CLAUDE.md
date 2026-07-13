# CLAUDE.md

> **Read first:** [`STYLEGUIDE.md`](./STYLEGUIDE.md) — code conventions, follow on every change.
>
> **Plan / roadmap:** [`DESIGN.md`](./DESIGN.md) — full design and the phased build plan
> (S0 → M13). Check it for what's being built and in what order before starting work.

Project-specific rules only. The universal working rules — boundaries, when-stuck,
secrets, done-honesty — live in the global `~/.claude/CLAUDE.md` and apply underneath
this. Only add a rule here when it differs from, or isn't covered by, the global.

## Run story (host-native)

Core runs natively on this machine — no core container. Fresh-machine setup is the
one-liner (`bootstrap.sh` → `install.sh`, README.md); from a clone, `./install.sh`
does prereqs, scaffold, deps, migrations, the first-run wizard (web password + model
auth — no platform tokens), the `chief` launcher, and the autostart service
(`--google` / `--playwright` add the MCP sidecars; `--no-service` / `--no-launch` /
`--non-interactive` for automation; re-runs are idempotent). Run in the foreground
with `chief run` or `uv run python -m chief.entrypoint`; lifecycle via
`chief start|stop|status|update|uninstall` — releases are git tags, and `update`
pins to the newest. Sidecars: `docker compose --profile google up -d`.
Attach the terminal client with `uv run chief-cli` (`--socket` overrides
`settings.socket_path`); it is only a client, so quitting it leaves the daemon running.
The web UI (#153) serves in-process at `http://127.0.0.1:8130` by default (`web_*`
settings; `web_lan_enabled` opens it to the LAN) — owner-password cockpit, server-
rendered htmx + SSE, no Node toolchain.

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
