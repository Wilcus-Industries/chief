# EXTENDING — recipes

Each recipe names the files to touch, the pattern to mirror, and the test to add.
Every one ends the same way: run the done-check.

```
uv run pytest && uv run ruff check . && uv run mypy .
```

Two repo-wide constraints that shape every recipe:

- **Production files stay under 200 lines — hard cap, CI-enforced** by
  `tests/test_styleguide.py`. Exceed it only with a
  `# styleguide: file-length — <justification>` comment in the first 5 lines.
- **Tool handlers return error strings; they never raise.**

## Add a native tool

1. Write the `ToolSpec` + async handler in a `register_<name>_tools(registry, ...)`
   function. **Mirror `src/chief/monitors/tools.py`** — one tool, an `action`
   enum, per-action inner coroutines, validation returning error *strings*.
2. Call it from `register_native_tools` in `src/chief/toolset.py`, passing deps
   through from `build_app`.
3. Set `wants_context=True` if the handler needs `thread_key` / `channel`.
4. Add an integration test that dispatches a **real `ToolCall` through a real
   `ToolRegistry`** (see `tests/test_session_tools.py`).

**`read_only=True` is a security decision**, not a convenience flag — it means the
gate auto-approves without asking. It means "a pure read: no filesystem write, no
network, no state, no send." Only three tools set it today. When in doubt, leave
it False and let the gate ask.

## Add an owner slash command

Add a handler method to `CommandSet` in `src/chief/commands.py` and register it in
the `_commands` map.

Commands run **before** the turn, which makes this the safe place to mutate the
thread you are in — unlike the `session` tool, which refuses its own thread.

A handler returning `str` sends deterministically and ends there. Returning a
`Message` **rewrites the turn** (this is how skill invocation works). Returning
`None` means "not a command."

## Add a store or session op

Add the method to `MessageStore` (`src/chief/persistence/store.py`), then expose
it through `SessionManager` (`src/chief/agent/manager.py`) if a tool or command
needs it. Touch `src/chief/persistence/models.py` only for a schema change —
and remember **there are no migrations**; the schema is created at boot.

If the op deletes or clears session state, route it through `SessionManager._wipe`
rather than writing to the store directly. `_wipe` holds the session lock across
the store write to close a TOCTOU, and drops the in-memory cache — a DB wipe alone
is cosmetic while a loaded `Session` still holds the old history.

## Add an MCP server

**There is no tool for this — it is config.** `edit_file` the `mcp_servers` key in
`config.yaml` (a `url` for HTTP, or a `command` argv list for stdio), then
`restart`.

Editing the dataclass default in `config.py` is a **silent no-op** — see the
dataclass-default trap in [CONFIG.md](./CONFIG.md).

The next boot connects it and its tools appear as `mcp_<name>_<tool>`. A server
that fails to start is logged and skipped — a dead sidecar must not keep the whole
daemon down. Reconnects re-register tools with `replace=True`.

## Add a classifier

Drop a `<name>.md` in `classifiers/` with frontmatter `name`, `description`,
`labels` (a YAML list), optional `model`; the body is the classification prompt.

**Quote `YES`/`NO` labels or rely on the label-safe loader** — plain YAML 1.1
coerces them to booleans. The loader handles it, but don't swap it for a stock
`SafeLoader`.

`validate()` runs as part of the self-edit done-check, so a malformed classifier
is rolled back rather than restarted into. There is no agent-facing tool;
core services call classifiers by name.

## Add a subagent

Drop a `<name>.md` in `agents/` with frontmatter `name`, `description`, optional
`tools` allowlist and `model`; the body is its system prompt.

Omitting `tools` grants every tool **minus `spawn_agent`** — the recursion guard is
hardcoded ahead of the allowlist, so a subagent can never spawn another one.

Subagent output does not stream; only the final text returns as the tool result.
Subagents never fire hooks.

## Add a skill

One directory per skill under `skills/`, containing `SKILL.md` — YAML frontmatter
(`name`, `description`) plus a body. The prompt carries only the one-liner
descriptions; `load_skill` pulls the full body on demand.

`validate()` runs in the self-edit done-check, so a placeholder body is rolled back.

Note that installed skills are **copies** of `packages/*/skills/*/SKILL.md`.
`chief update` re-syncs them, but only for skills already installed, and it
refuses to overwrite a locally edited copy (see [OPERATIONS.md](./OPERATIONS.md)).

## Add a package

**Default answer: it belongs in the chief-packages repo, not here.** `packages/`
in core is only for packages whose skills document core internals, where a split
repo would let the two skew. Adding one to this repo needs the owner's explicit
approval.

A package is a directory with `manifest.yaml`:

| Field | Type | Notes |
|---|---|---|
| `name` | str | defaults to dir basename |
| `description` | str | required by the validator |
| `skills` | list[str] | package-relative paths |
| `config_keys` | list[str] | top-level config keys the package owns |
| `secrets` | list[str] | filenames expected under `secrets/` |
| `python_deps` | list[str] | **import** names (`yaml`), never dist names (`PyYAML`) |
| `hooks` | mapping | `{module, register}` |
| `mcp_servers` | mapping | `{<name>: {url}}` or `{<name>: {command}}` — exactly one |

Plus `INSTALL.md` / `UNINSTALL.md`, and ideally an `install.sh` for the
deterministic path.

Install is **document-driven** — there is no install tool. The agent reads
`INSTALL.md` and executes it. The flow:

1. `chief-pkg search` → path
2. read `manifest.yaml` + `INSTALL.md`, gather owner-facing questions
3. run `install.sh` when present
4. set config keys **only** via `uv run python -m chief.config_apply key=value`
5. `restart`
6. `chief-pkg verify <name>` must print "fully installed"

Uninstall mirrors it, and the **final step deletes `UNINSTALL.md` itself — the
vanished file is the completion signal**.

Bundled beats cloned on a name collision (`packages/` is scanned before
`data/packages/`). Editing a bundled package needs `chief update`, not
`chief-pkg update`.

## Add a package hook

Hooks let a package contribute per-turn context without touching core. Declare a
`hooks:` block in the manifest (`module` + `register`); `build_app` scans
installed, non-disabled packages and calls each `register(context, hooks)`.

Four kinds:

```python
PreTurnHook      = Callable[[TurnContext], Awaitable[str | None]]
SessionStartHook = Callable[[TurnContext], Awaitable[str | None]]
PostTurnHook     = Callable[[TurnResult, list[dict[str, Any]]], Awaitable[None]]
PostToolHook     = Callable[[ToolCall, str], Awaitable[Annotate | Veto | None]]
```

`HookContext` gives a package `provider`, `models`, `config` (**sliced to just
that package's `config_keys`**), `data_dir` (`data/hooks/<name>/`), `logger`,
`budget`, and `classifier`. Deliberately **no** sessions, gate, or self-edit
pipeline.

Reach for `classifier` before `provider` for any categorical judgement: it is
the same primitive monitors use, so you get label validation and retries, and —
the point — the prompt lives in an owner-editable `classifiers/<name>.md`
instead of a string constant inside your package. Ship the definition in your
package and have `install.sh` copy it into `classifiers/` without overwriting an
existing file. `obsidian-memory`'s `memory-relevance` gate is the worked
example.

`TurnContext` carries `user_text`, `messages` (the transcript *before* the
incoming turn), `sender`, `thread_key`, and `channel`.

Rules that matter:

- Every hook runs under `asyncio.wait_for`. A raise or timeout is logged and the
  contribution is **dropped**; siblings are unaffected. Don't rely on a hook
  always landing.
- Blocks are **sorted by package name** before rendering, so placement is
  deterministic regardless of load order.
- A `post_tool` hook may **annotate** (a `<hook>` note above the payload) or
  **veto** (the payload is withheld, the model gets your reason). It cannot
  rewrite the payload — any other return value is ignored, and the first veto
  wins.
- Contributed text **and tool payloads** are escaped — `<hook>` delimiters are
  entity-escaped and the source attribute is slugged. **Both may be
  attacker-influenced** (retrieved or relayed content), so the delimiter and
  attribution are trusted structure neither can forge: a fetched page cannot
  ship its own `<hook source="screener">` and impersonate the screener. Apart
  from those delimiters, an annotated payload reaches the model byte-for-byte.
- Keep the hook module import-light. It is imported at boot; do expensive imports
  lazily inside the hook body. `packages/obsidian-memory/hooks.py` is the
  reference shim.
- **Register the package** with `python -m chief.registry_apply <name>` or its
  hooks never load, even though its skills will answer — the half-install failure
  mode.

## Working with file tools and shell

`read_file` and `grep` are `read_only`. `write_file` (whole-file replace) and
`edit_file` (replaces **every** occurrence of `old`) are not.

**Writes have no repo confinement and no carve-outs** — `secrets/`, `.git/`,
`data/`, and off-repo paths are all reachable. Edits are inert until `restart`
runs the done-check. The approval gate is the only guard on where writes land.

The shell is one long-lived subprocess **per thread**, so `export`/`cd`/jobs
persist across commands. Notable behavior:

- Default timeout is 20s; pass the `timeout` arg for genuinely slow work
  (installs, clones, builds).
- A killed command returns **exit code 124 and loses its shell state** — a fresh
  shell spawns for the next command.
- Exit `137` means the shell died before the sentinel; `2` is a syntax error
  caught by a pre-flight `-n` check. That pre-flight exists because a command
  that leaves the shell mid-parse (unterminated quote, open here-doc) would
  swallow the sentinel and hang until timeout, losing the session over a typo.
  It is a fast-fail heuristic, **not** a security boundary — the gate is.
- The sentinel is randomized per process so command output can't spoof the
  boundary.
- `shell` is not `read_only`, so it raises an approval card until "always allow".
  It also carries `owner_send_guard`, blocking `imsg`/`osascript` commands that
  reference an owner handle (the iMessage echo-loop seatbelt).
