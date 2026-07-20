# CONFIG — keys and on-disk layout

## How config resolves

`config.yaml` (install-local, gitignored) is seeded from `config.default.yaml`
(tracked template) on first install. `load_raw` returns `{}` when the file is
absent; `load_config` then constructs `Config` by passing **every field
explicitly**.

`_StrictLoader` raises `ConfigError` on any **duplicate mapping key** — PyYAML
would silently keep the last one, and three appended `obsidian_memory:` blocks in
production motivated the guard.

Precedence per key: `CHIEF_<KEY>` env var → `config.yaml` → the literal in the
`Config(...)` call.

### The dataclass-default trap

Because `load_config` passes all fields explicitly, **every
`field(default_factory=...)` on the dataclass is dead at runtime**. The real
default is the inline literal in the `Config(...)` call.

`mcp_servers` is the canonical case: no env override, no template entry, and
`mcp_servers=dict(raw.get("mcp_servers") or {})`. Editing the `default_factory`
in `config.py` is a **silent no-op**. To add an MCP server you edit `config.yaml`
and restart. A stdio entry may also declare `env` (a `dict[str, str]`) and `cwd`
(a string path) — `env` is opt-in, not inherited: the child process gets the
MCP SDK's minimal default set (`PATH`, `HOME`, …) plus exactly the keys listed
here, never the daemon's own environment. Omitting both behaves exactly as
before. An entry may also declare `timeout` (seconds, any numeric type) —
how long `McpManager.connect` waits for that one server to become ready
before failing loudly; a server that resolves or builds dependencies on its
first launch can outrun the default. Omitting it gets
`DEFAULT_CONNECT_TIMEOUT_SECONDS` (30s, `chief.mcpclient.manager`). A
timed-out connect cancels its supervising task and raises naming the server;
the daemon logs it and keeps booting the rest.

The same applies to `web_host`, `web_port`, `skills_dir`, `agents_dir`,
`classifiers_dir`, and `imessage.db_path` — all have dataclass defaults but no
template line.

## Key table

| YAML key | Field | Type | Default | Env override |
|---|---|---|---|---|
| `models.<role>` | `models` | `dict[str,str]` | `{"default": "qwen/qwen3-coder"}` | — |
| `models.temperature` | `temperature` | `float` | `0.0` | — |
| `provider_base_url` | `provider_base_url` | `str` | OpenRouter v1 | `CHIEF_PROVIDER_BASE_URL` |
| `provider_backends.<n>` | `provider_backends` | `dict[str,BackendSpec]` | `{}` | via `api_key_env` |
| `provider_aliases.<n>` | `provider_aliases` | `dict[str,AliasSpec]` | `{}` | — |
| `db_path` | `db_path` | `Path` | `data/chief.db` | `CHIEF_DB_PATH` |
| `socket_path` | `socket_path` | `Path` | `data/chief.sock` | `CHIEF_SOCKET_PATH` |
| `max_concurrent_sessions` | same | `int` | `4` | `CHIEF_MAX_CONCURRENT_SESSIONS` |
| *(secret file)* | `openrouter_api_key` | `str` | `""` | `OPENROUTER_API_KEY` |
| `gate.never` | `gate_never` | `tuple` | `()` | — |
| `gate.approved` | `gate_approved` | `tuple` | `()` | — |
| `gate.announce` | `gate_announce` | `bool` | `True` | — |
| `budget.cap_usd` | `budget_cap_usd` | `float` | `0.0` (unlimited) | — |
| `budget.warn_ratio` | `budget_warn_ratio` | `float` | `0.8` | — |
| `quiet_hours` | `quiet_hours` | `str` | `""` | — |
| `web_host` | `web_host` | `str` | `127.0.0.1` | `CHIEF_WEB_HOST` |
| `web_port` | `web_port` | `int` | `8130` | `CHIEF_WEB_PORT` |
| *(secret file)* | `web_password` | `str` | `""` | `CHIEF_WEB_PASSWORD` |
| `skills_dir` | `skills_dir` | `Path` | `skills` | `CHIEF_SKILLS_DIR` |
| `agents_dir` | `agents_dir` | `Path` | `agents` | `CHIEF_AGENTS_DIR` |
| `classifiers_dir` | `classifiers_dir` | `Path` | `classifiers` | `CHIEF_CLASSIFIERS_DIR` |
| `mcp_servers.<n>` | `mcp_servers` | `dict[str,dict]` | `{}` | — |
| `packages_dir` | `packages_dir` | `Path` | `packages` | `CHIEF_PACKAGES_DIR` |
| `packages_repo` | `packages_repo` | `str` | chief-packages repo | `CHIEF_PACKAGES_REPO` |
| `shell.timeout_seconds` | `shell_timeout_seconds` | `float` | `20.0` | — |
| `shell.output_limit` | `shell_output_limit` | `int` | `30000` | — |
| `hooks.timeout_seconds` | `hooks_timeout_seconds` | `float` | `10.0` | — |
| `hooks.disabled` | `hooks_disabled` | `tuple` | `()` | — |
| `imessage.enabled` | `imessage_enabled` | `bool` | `False` | — |
| `imessage.owner_handles` | `imessage_owner_handles` | `tuple` | `()` | — |
| `imessage.db_path` | `imessage_db_path` | `Path` | `~/Library/Messages/chat.db` | — |
| `imessage.poll_seconds` | `imessage_poll_seconds` | `float` | `2.0` | — |

`Config.default_model` is a derived property: `self.models["default"]`.

Notes on specific keys:

- **`models` is merged, not replaced** — `dict(Config().models) | raw_models`.
- **`temperature` is popped out of the `models` block**, not a sibling key.
- **Secrets never come from YAML.** `openrouter_api_key` and `web_password` read
  env first, then `secrets/<name>`. Empty `web_password` means the web UI fails
  closed and no listener is built.
- **`gate.approved` accepts `"*"`** to approve every tool; `gate.never` still wins.
- **`imessage.enabled` also requires `sys.platform == "darwin"`.**
- **`quiet_hours` is `"HH:MM-HH:MM"`** and may span midnight; prompt-waking
  schedule fires inside the window defer to its end (command schedules run
  silently and are never deferred).

### The owner_handles coercion trap

`_as_handles` turns `None`/`""` into `()`, a `str` into a 1-tuple, and a list into
per-element strings — but **raises `ConfigError` on an int or mapping**. This
matters because a bare `owner_handles: +15551234567` parses as the integer
`15551234567`, silently dropping the `+`. Quote handles.

## Writing config programmatically

- **`merge_config(updates, path)`** deep-merges and rewrites `config.yaml`.
  Recurses into dict/dict, replaces everything else.
- **`python -m chief.config_apply dotted.key=<yaml-value>`** is the CLI package
  installers must use. The RHS goes through `yaml.safe_load`, so bools, numbers,
  and lists work. It exists so `install.sh` scripts set keys byte-exactly instead
  of the model retyping YAML (#185).

**`config.yaml` is gitignored, so the self-edit seatbelt cannot roll back a bad
config write.** The pre-restart config gate is the only protection. Never
hand-edit config from a package install; use `config_apply`.

## On-disk layout

### `data/` — gitignored wholesale, all daemon-written

| Path | Contents |
|---|---|
| `chief.db` | sqlite sessions + transcripts; schema created at boot, **no migrations** |
| `chief.sock` | unix socket for `chief-cli` |
| `audit.jsonl` | every gated tool call |
| `gate_approved.json` | persisted "always allow" set, minus config-approved |
| `installed.yaml` | package install registry |
| `packages/` | clone of `packages_repo`, pulled by `chief-pkg update` |
| `hooks/<package>/` | per-package hook scratch state |
| `system.md` | self-edited system prompt override |
| `memory/` | soul + memory vault (its own git repo when the package is installed) |
| `restart_notice.json` | the thread that asked for a restart |
| `chief.log` | daemon stdout/stderr on macOS / `--no-service` (systemd uses journald) |

Also gitignored at repo root: `.selfedit-pending.json` — the rollback marker.
Runtime state that must survive restart and must never be committed.

### `secrets/` — `secrets/*` ignored, `README.md` tracked

One file per secret, no extension, `chmod 0600` by the wizard. Core files:
`openrouter_api_key`, `web_password`. Package secrets land here too.
`provider_backends[*].api_key_secret` resolves relative to this directory.

### Tracked directories

- **`skills/`** — one dir per skill with `SKILL.md`. Installed skills are
  **copies** of `packages/*/skills/*/SKILL.md`, re-synced by `chief update`.
- **`packages/`** — bundled packages, in core's git repo. Adding one here needs
  the owner's explicit approval; the default home is the chief-packages repo.
- **`agents/`** — one `<name>.md` per subagent.
- **`classifiers/`** — one `<name>.md` per classifier.

### `backups/` — gitignored

Memory tar dumps and credential backups. Not written by the daemon.

### `config.yaml` vs `config.default.yaml`

`config.default.yaml` is the tracked, heavily commented template, never read at
runtime except as the seed. `config.yaml` is gitignored specifically so wizard and
agent edits don't leave the tree permanently dirty — the self-edit pipeline
refuses to run against a dirty tree. The tradeoff is that the seatbelt can't roll
a bad config write back.

## The package registry — `registry_apply.py`

`data/installed.yaml` has shape `{name: {source: bundled|cloned}}` and is owned
exclusively by this module.

`load_installed()` is the single reader shared by boot, discovery, and verify.
Missing file → `{}`. A YAML error or non-mapping → **logged at ERROR** and `{}`.
The loud degradation is deliberate: an empty registry turns **every** package's
hooks off at once.

`apply()` is an idempotent read-modify-write that preserves other entries. CLI:
`python -m chief.registry_apply <name> [--source bundled|cloned] [--remove]`.

**Half-install detection:** the hook loader warns when a package's skills landed
at `skills/<name>/SKILL.md` but no registry entry exists — the failure mode where
the package appears to work (skills answer) while its hooks never load.
