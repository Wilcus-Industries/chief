# ARCHITECTURE — a map of yourself

You (chief) can edit your own source. Before you do, read this: it names where
each capability lives so you change the right file instead of re-deriving the
layout every time. Paths are relative to the repo root. Edit with `write_file` /
`edit_file` (they reach the whole filesystem, no carve-outs), then `restart` to
run the done-check and — on green — commit and reboot into the change. Read a
file before you change it.

## Where things live

- **Sessions / transcripts** — persisted in sqlite at `data/chief.db`. The
  schema (`SessionRow`, `MessageRow`) is in `src/chief/persistence/models.py`;
  every read/write op (`ensure_session`, `list_sessions`, `load`, `append`,
  `clear`, `delete_session`, …) is a method on `MessageStore` in
  `src/chief/persistence/store.py`. `SessionManager`
  (`src/chief/agent/manager.py`) wraps the store with an in-memory live-session
  cache.
- **Config keys** — `src/chief/config.py` (the `Config` dataclass + its
  env/YAML loader, which reads `config.yaml`). Add a new key here. The MCP
  server list, though, is a *runtime value*: `load_config` always fills
  `mcp_servers` from the `mcp_servers` key in `config.yaml`, so that is the
  file you edit to add a server (see the recipe), not the dataclass default.
- **System prompt** — the boot-static base is in `src/chief/agent/prompt.py`
  (`system_prompt` + `ONBOARDING_SUFFIX`); skill one-liners are appended in
  `chief.app._build_prompt`. The *per-turn* message (soul on top, then base +
  origin note, then package hook blocks) is assembled fresh each turn in
  `Session._assemble_system` via `chief.hooks.runner.assemble_system`.
- **Agent-loop hooks** — packages contribute per-turn context (`pre_turn`),
  first-turn context (`session_start`), and turn observers (`post_turn`) through
  `src/chief/hooks/`: `registry.py` (the store), `runner.py` (resilient
  timeout-bounded execution + the `<hook source="…">` render and system
  assembly), `context.py` (the `HookContext` a hook registers with, plus the
  per-turn `TurnContext` — inbound text, recent transcript, sender, thread,
  channel — its context hooks receive so they can judge relevance and gate on
  the sender), and `loader.py` (the boot importer). A package opts in with a `hooks:`
  block in its manifest (`module` + `register`); `build_app` scans installed,
  non-`hooks.disabled` packages and calls each one's `register(context, hooks)`.
  Only `Session._one_turn` fires hooks — subagents never do.
- **Skills** — one directory per skill under `skills/`, each a `SKILL.md` (YAML
  frontmatter + body). Loader/validator: `src/chief/skills.py`. The prompt
  carries only the one-liners; `load_skill` pulls a full body.
- **Native tools** — the registry and dispatch live in
  `src/chief/agent/tools.py`. The default set is assembled once by
  `register_native_tools` in `src/chief/toolset.py` and wired at boot in
  `src/chief/app.py` (`build_app`). Each tool's spec + handler lives with its
  feature (e.g. `src/chief/monitors/tools.py`, `src/chief/agent/session_tools.py`).
  The host `shell` tool is `src/chief/shelltool.py` (surface) over
  `src/chief/shellhost.py` + `src/chief/shellframe.py` (the persistent-shell driver);
  `ShellService` is owned by `App` and closed at shutdown.
- **Owner slash commands** — `CommandSet` in `src/chief/commands.py`
  (`/help`, `/clear`, `/prune`, `/model`, …). These run *before* a turn.
- **Packages** — bundled under `packages/`, cloned ones under `data/packages/`;
  loader is `src/chief/packages.py`. Discover with the `chief-pkg` CLI run through
  the `shell` tool (`src/chief/pkgcli.py`: `list` / `search`, `--installed`).
  Install/uninstall
  are document-driven — follow the package's `INSTALL.md` / `UNINSTALL.md` with
  your file tools, record installs in `data/installed.yaml`, then `restart`.
- **Restart pipeline (the self-edit seatbelt)** — `src/chief/selfedit/`
  (`pipeline.py` runs the done-check against your working tree, commits + writes
  the rollback marker on green, keeps your edits on red; `recovery.py` handles
  restart/rollback; `notice.py` records the thread that asked, so the rebooted
  daemon reports "restart success" — or the rollback — back there). The
  `restart` tool is your only way to make edits live.
- **Boot wiring** — `src/chief/app.py` (`build_app`, the table of contents) with
  the infrastructure phases (persistence, gate, MCP, adapters) in
  `src/chief/wiring.py`. The entrypoint (`src/chief/entrypoint.py`) takes a
  single-instance `flock` (`src/chief/instance_lock.py`) before `build_app` so a
  stray second daemon can't double-poll `chat.db` and double-answer.

## Recipes

**Add a native tool.** Write its `ToolSpec` + async handler in a
`register_<name>_tools(registry, ...)` function (mirror
`src/chief/monitors/tools.py`: one tool, an `action` enum, per-action inner
coroutines, validation returns error *strings* — never raise). Call it from
`register_native_tools` in `src/chief/toolset.py`, passing any deps through from
`build_app`. Add an integration test that dispatches a real `ToolCall` through a
real `ToolRegistry` (see `tests/test_session_tools.py`).

**Add an owner command.** Add a handler method to `CommandSet` in
`src/chief/commands.py` and register it in the `_commands` map. It runs before
the turn, so it is the safe place to mutate the thread you are in (unlike the
`session` tool, which refuses your own thread).

**Add a store / session op.** Add the method to `MessageStore`
(`src/chief/persistence/store.py`); expose it through `SessionManager`
(`src/chief/agent/manager.py`) if a tool or command needs it. Adjust
`src/chief/persistence/models.py` only for a schema change.

**Add an MCP server.** No tool for this — it is config. `edit_file` the
`mcp_servers` key in `config.yaml` (a `url` for HTTP, or a `command` argv list
for stdio) — editing the dataclass default in `config.py` is a silent no-op,
since `load_config` always reads the value from `config.yaml` — then `restart`.
The next boot connects it; its tools appear as `mcp_<name>_<tool>`. (See the
self-edit skill for the seatbelt details.)
