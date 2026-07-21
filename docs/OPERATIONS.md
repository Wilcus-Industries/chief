# OPERATIONS — install, service, update

How chief gets onto a machine and how it moves forward. Host-native: no
containers, no migrations — the schema is created at boot by the new tree's code.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/CrazyWillBear/chief/main/bootstrap.sh | bash
```

`bootstrap.sh` accepts `--dir DIR`, `--ref TAG`, `--no-service`, `--no-launch`,
`--non-interactive`, and does:

1. **Detect OS** — Darwin, or Linux with a debian-family `ID`/`ID_LIKE`. Anything
   else Linux gets a best-effort warning; anything else fails.
2. **Prereqs** — Homebrew (macOS), git, uv.
3. **Fetch** — clone to `~/.local/share/chief` (or `--dir`), checkout the newest
   tag or `--ref`. **A dirty tree skips the checkout with a warning** and never
   destroys local state.
4. `exec bash ./install.sh`.

`install.sh` is idempotent and does prereq checks → `mkdir data secrets` →
`uv sync` → wizard → launcher → service → launch. From a clone, run it directly to
skip the bootstrap.

The launcher written to `~/.local/bin/chief` bakes in the repo dir and uv path,
`cd`s to the repo, and dispatches: `run` execs the entrypoint directly, everything
else execs `python -m chief.install <cmd>`.

### The wizard

Three idempotent steps, each reporting `set` / `kept` / `skipped`:

- **Password** — keeps an existing `secrets/web_password`, else `CHIEF_OWNER_PASSWORD`
  (min 8 chars), else prompts with confirmation. Non-interactive with no env
  means **the web UI stays locked** — fail closed.
- **API key** — keeps an existing key or `OPENROUTER_API_KEY`, else
  `CHIEF_OPENROUTER_KEY`, else prompts. Every candidate is validated with one real
  `GET /api/v1/key` call (10s timeout).
- **Budget** — regex-matches the `cap_usd:` line in `config.yaml` and **splices it
  back in place**, preserving comments. It is not a YAML rewrite. No match means
  skip; an existing value `> 0` is kept.

Re-run any time with `chief wizard`.

## Commands

```
chief status     # service + web UI state
chief update     # merge origin/main, resync skills, restart, roll back if unhealthy
chief start      # / stop
chief run        # foreground, instead of the service
chief wizard     # re-run the first-run wizard
chief uninstall  # remove service + launcher; --purge-data removes data too
```

`status` prints the service state, then probes the web URL with a 2s health wait.
`uninstall` with `--purge-data` and no `--yes` prompts before deleting `data/`
and `secrets/`; without `--purge-data` it explicitly says both were kept.

## The service

macOS uses a launchd agent (`com.chief.daemon`, plist in `~/Library/LaunchAgents`);
Linux uses a systemd user unit (`chief.service` in `~/.config/systemd/user`).
Both set an explicit `PATH` so the service can reach `uv`.

launchd uses `KeepAlive={"SuccessfulExit": False}` — restart on crash, but let
`chief stop` actually stop it. Linux additionally attempts `loginctl enable-linger`
best-effort; a failure only warns (it autostarts at login, but not before you log
in after a reboot).

### Restart is `kickstart -k`, never stop-then-start

On macOS, `restart` uses `launchctl kickstart -k` — SIGKILL and relaunch,
atomically. Only if that fails (label not loaded) does it bootstrap then kickstart.

**A bootout-first graceful drain is the documented outage.** It leaves an orphan
half-holding the label, the follow-up bootstrap fails, and the daemon stays down
while the caller sees a clean return. Don't reintroduce it.

`install` also boots out first before writing the plist, because bootstrapping an
already-loaded label errors.

## What `chief update` does

In order, with the failure behavior that matters:

1. `git fetch --force origin` — non-zero returns 1.
2. Record `before = HEAD`, `target = origin/main`.
3. **Up-to-date test is `git merge-base --is-ancestor origin/main HEAD`, not
   `before == target`** — a self-editing install commits to this same repo, so
   HEAD is permanently ahead of origin/main and equality never holds.
4. **Dirty guard** on tracked files only. Untracked files are expected (installed
   skills live untracked on some installs), but a modified tracked file would be
   silently clobbered by `-X theirs`, so a non-empty list prints and returns 1.
5. `git merge -X theirs --no-edit origin/main`; failure aborts the merge and
   returns 1 with the tree untouched. **It merges, never checks out a tag** — a
   tag checkout would drop the install's local self-edit commits out of the
   working tree.
6. `uv sync`; failure rolls back.
7. **Re-sync installed skills.** For each `packages/*/skills/*/SKILL.md`: skip
   names whose `skills/<name>/` doesn't exist (an update must never enable a
   capability the owner didn't install); skip byte-identical targets; otherwise
   compare the target against `git show <before>:<path>`. Equal means safe to
   copy; different means **drifted — left alone** and reported as "NOT overwritten
   (locally edited)".
8. If anything synced, commit it (`chore(skills): re-sync after update`) — because
   `skills/` is tracked, so leaving copies modified would trip step 4's own dirty
   guard on the next run.
9. No service installed → print "restart chief yourself" and return 0.
10. `service.restart()`, then health check.
11. **Health is service-first, then web.** A stopped or not-installed service is
    unhealthy immediately; otherwise wait up to 45s on the web URL. A web probe
    alone is insufficient — right after a restart the *old* process can still be
    serving while the new one never comes up.
12. Unhealthy → `git reset --hard <before>`, `uv sync`, restart. Rollback always
    returns 1, and a failed reset reports `ROLLBACK FAILED`.

Health checks count **any status < 500** as healthy, so a login redirect is
healthy.

Cloned packages update separately via `chief-pkg update`, which restarts nothing.

### There are no migrations — new columns are a manual deploy step

The schema is created at boot, so an update that adds a column leaves an existing
`data/chief.db` without it, and every read of that table raises `no such column`.
Run the `ALTER TABLE` by hand at deploy; each such commit says which one in its
message (most recently `ALTER TABLE schedules ADD COLUMN command VARCHAR`).

The cron loop survives this rather than dying silently — a failed tick is logged
and retried on the next poll — but **schedules stay broken until you run the
ALTER**, so treat the log line as the alarm it is.

## Package CLI — `chief-pkg`

```
chief-pkg list [--installed]
chief-pkg search <query> [--installed]
chief-pkg update
chief-pkg verify <name>
```

`verify` is the install postcondition: registry entry present, each skill at
`skills/<basename>/SKILL.md`, each config key in raw config, each secret file
present, each `python_deps` entry importable, each declared `mcp_servers` entry
present under `mcp_servers` in `config.yaml`. Exits 1 with a problem list.

**Every invocation refreshes the clone** (clone-if-missing, then pull), and both
are bounded and fail-soft — 30s clone, 10s pull, `GIT_TERMINAL_PROMPT=0`. The
reason: `chief-pkg` runs through the single dispatcher, so a hanging git command
hangs the whole daemon. A stale clone is always the better failure.
