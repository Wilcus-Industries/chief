# ARCHITECTURE — a map of yourself

You (chief) can edit your own source. This is the index: it names where each
capability lives so you change the right file instead of re-deriving the layout
every time. Human contributors and Claude Code sessions read the same map.

Edit with `write_file` / `edit_file` (they reach the whole filesystem, no
carve-outs), then `restart` to run the done-check and — on green — commit and
reboot into the change. **Read a file before you change it.**

Symbols are named rather than line-pinned throughout these docs. This repo edits
itself, so line numbers rot; grep the symbol.

## Where to look

| Doc | Covers |
|---|---|
| [LIFECYCLE.md](./LIFECYCLE.md) | inbound message → bus → dispatch → session → turn → tool loop → reply |
| [SECURITY.md](./SECURITY.md) | trust model, the gate, approvals, strangers, audit, budget, locks |
| [SUBSYSTEMS.md](./SUBSYSTEMS.md) | web UI, monitors, classifiers, subagents |
| [CONFIG.md](./CONFIG.md) | every config key, on-disk layout, the package registry |
| [EXTENDING.md](./EXTENDING.md) | recipes: tools, commands, packages, skills, hooks, MCP |
| [OPERATIONS.md](./OPERATIONS.md) | install, the service, `chief update`, `chief-pkg` |
| [TESTING.md](./TESTING.md) | done-check, fixtures, the provider fake, meta-tests |

Working rules live in [`CLAUDE.md`](../CLAUDE.md); code conventions in
[`STYLEGUIDE.md`](../STYLEGUIDE.md); the rewrite's target design in
[`DESIGN.md`](../DESIGN.md).

## The shape of the thing

A minimal core — agent loop, sessions, events, gate, budget — plus installable
packages for everything else. Core runs natively on the host: no containers, no
migrations (the schema is created at boot).

```
adapters ──→ Dispatcher ──→ Session ──→ agent loop ──→ tools ──→ reply
   │             │                          │            │
   │             └──→ EventBus ──→ monitors │            └──→ gate → approvals
   │                                        └──→ hooks (packages)
   └── imessage · socket/cli · web
```

Every inbound path converges on `Dispatcher.handle`, and every reply leaves
through the adapter named on the `Message` that started it.

## Where things live

- **Sessions / transcripts** — sqlite at `data/chief.db`. Schema (`SessionRow`,
  `MessageRow`) in `src/chief/persistence/models.py`; every read/write op is a
  method on `MessageStore` in `src/chief/persistence/store.py`. `SessionManager`
  (`src/chief/agent/manager.py`) wraps the store with a live-session cache.
- **Config keys** — `src/chief/config.py` (the `Config` dataclass + loader). Add
  a key here. **But note the dataclass-default trap** in [CONFIG.md](./CONFIG.md):
  `load_config` passes every field explicitly, so editing a `default_factory` is
  a silent no-op. `mcp_servers` in particular is a runtime value read straight
  from `config.yaml`.
- **System prompt** — the boot-static base is `src/chief/agent/prompt.py`
  (`system_prompt` + `ONBOARDING_SUFFIX`); skill one-liners are appended in
  `chief.app._build_prompt`. The *per-turn* message (soul on top, then base +
  origin note, then package hook blocks) is assembled fresh each turn in
  `Session._assemble_system`. **It is never persisted**, so prompt and package
  edits apply to existing threads on the next turn.
- **Agent-loop hooks** — `src/chief/hooks/`: `registry.py` (the store),
  `runner.py` (timeout-bounded execution, `<hook>` rendering, system assembly),
  `context.py` (`HookContext` and the per-turn `TurnContext`), `loader.py` (the
  boot importer). A package opts in with a `hooks:` block in its manifest. **Only
  `Session._one_turn` fires hooks — subagents never do.**
- **Skills** — one directory per skill under `skills/`, each a `SKILL.md`.
  Loader/validator: `src/chief/skills.py`. The prompt carries only one-liners;
  `load_skill` pulls a full body.
- **Native tools** — registry and dispatch in `src/chief/agent/tools.py`. The
  default set is assembled by `register_native_tools` in `src/chief/toolset.py`
  and wired at boot in `src/chief/app.py`. Each tool's spec + handler lives with
  its feature (`src/chief/monitors/tools.py`,
  `src/chief/agent/session_tools.py`). The host `shell` tool is
  `src/chief/shelltool.py` over `src/chief/shellhost.py` +
  `src/chief/shellframe.py`.
- **Owner slash commands** — `CommandSet` in `src/chief/commands.py` (`/help`,
  `/clear`, `/prune`, `/model`, …). These run *before* a turn.
- **Packages** — bundled under `packages/`, cloned under `data/packages/`; loader
  is `src/chief/packages.py`. Discover with `chief-pkg` (`src/chief/pkgcli.py`).
  Install/uninstall are **document-driven** — follow the package's `INSTALL.md`
  with your file tools, record installs via `chief.registry_apply`, then
  `restart`.
- **Boot wiring** — `src/chief/app.py` (`build_app`, the table of contents) with
  infrastructure phases in `src/chief/wiring.py`. The entrypoint
  (`src/chief/entrypoint.py`) takes a single-instance `flock` before `build_app`
  so a stray second daemon can't double-poll `chat.db` and double-answer.

## Self-edit: the restart pipeline

`src/chief/selfedit/` is the seatbelt:

- `pipeline.py` runs the done-check against your working tree, commits and writes
  the rollback marker on green, and **keeps your edits in place on red** so you
  can fix forward.
- `recovery.py` handles restart and rollback.
- `notice.py` records the thread that asked, so the rebooted daemon reports
  "restart success" — or the rollback — back to the right place.

The `restart` tool is the only way to make edits live. Python does not reload live
imports, so a half-finished tree cannot hurt the running daemon.

**The exec fires at the outermost boundary** — the dispatcher for socket/web, the
iMessage worker after the cursor is durable — never inside `Session.run_turn`
where the reply is still unsent. New turns are held while in-flight ones drain,
bounded by a timeout, then `os.execv`.

Rollback covers **repo files only**. Writes into `data/`, `secrets/`, or outside
the repo are gated but unversioned. `config.yaml` is gitignored, so a bad config
write has no rollback — use `python -m chief.config_apply` rather than
hand-editing.

The full working procedure — including `revert_edits` and what to do after three
red checks in a row — is in the `self-edit` skill. Load it before a self-change.

## Definition of done

```
uv run pytest && uv run ruff check . && uv run mypy .
```

Production files stay **under 200 lines** — a hard, CI-enforced cap. See
[TESTING.md](./TESTING.md) for the meta-tests that guard this doc set itself.
