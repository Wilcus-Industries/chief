# OPERATIONS — install, service, update

How chief gets onto a machine and how it moves forward. Host-native: no
containers, no migrations — the schema is created at boot by the new tree's code.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/Wilcus-Industries/chief/main/bootstrap.sh | bash
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
`uv sync` → wizard → **dedicated account** → launcher → service → launch. From a
clone, run it directly to skip the bootstrap. `--single-user` skips the account
offer; `--non-interactive` never offers it at all.

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

### The dedicated system account

Default on, both platforms, **interactive only** — a scripted run refuses rather
than adding a system user unattended. Runs *after* the wizard on purpose: the
plan chowns the tree and locks down `secrets/`, and the wizard is what writes
the files in there.

Three cases, asked once: **create** the account (default, name overridable),
install into an account that **already exists**, or **decline** and get exactly
today's single-user install. Declining anywhere — the first question, or the
confirmation before anything runs — lands on the same supported fallback.

Everything it would run is printed first, then executed step by step, stopping
at the first failure. `sudo` prompts on the **terminal**, not on stdin, which is
what lets this work through `curl … | bash`. Passwords never appear in an argv;
they go in on stdin. The whole plan is pure data in `install/account.py` +
`install/account_steps.py` and is pinned byte-for-byte in `tests/test_install.py`.

What the plan covers: the account (non-admin — chief must log in graphically,
and an admin chief would be root-equivalent via its shell tool), a shared group
with both users in it, the tree chowned to chief and group-writable with setgid
inheritance, `secrets/` carved back out chief-only, chief's git identity
(self-edit commits fail outright without one), `safe.directory` in the owner's
git config (git refuses to work on a tree owned by another user), lingering on
Linux, and the file grants the wizard asked about.

Taking the account also sets `imessage.mode: dedicated` in `config.yaml`.

Run it on its own with `chief account` (or `python -m chief.install account`).

### The login session — why the install ends without starting the daemon

Messages only delivers into a **real graphical session**, and there is no
supported or unsupported-but-working way to manufacture one for a chosen user
from a root context at boot. So the installer detects the machine's
disk-encryption state (`fdesetup status`) and picks:

- **Unencrypted** → the supported automatic-login path
  (`sysadminctl -autologin set`). Chief owns the console; the owner switches to
  their own account; unattended reboots survive. macOS refuses this when the
  account's login password matches its Apple ID password, so the installer
  checks and re-asks.
- **Encrypted, or an unreadable state** → screen sharing is enabled and the
  reconnect is a **documented human step**: after each reboot, unlock the disk
  at the console as yourself, then connect Screen Sharing to `vnc://127.0.0.1`
  and log in as chief. That login is what creates the session. An unreadable
  state takes this branch on purpose — automatic login on an encrypted disk
  silently does nothing.

Because chief has no session yet at install time, a launchd agent cannot be
bootstrapped into one. The installer therefore **writes the service definition
into chief's home and exits without starting anything**, printing the remaining
steps. The agent loads at chief's first login.

### The boot check

Automatic login is documented to break silently after an OS update, so the state
is reported rather than inferred from chief going quiet. `read_posture()`
(`install/posture.py`) probes the encryption setting, the automatic-login
setting and whether a GUI domain exists for the uid, and names the failure
modes. It surfaces in exactly two places, both required and neither a push
channel: `chief status`, and the web statusbar (polling `/posture` every 10s,
red when not `ok`).

## Commands

```
chief status     # service + web UI state
chief update     # apply the newest release onto this box's own edits
chief check-updates  # is a newer core release out? (changes nothing)
chief release <part>  # cut a release — upstream repo only, never a box
chief start      # / stop
chief run        # foreground, instead of the service
chief wizard     # re-run the first-run wizard
chief account    # give chief its own system user (interactive only)
chief uninstall  # remove service + launcher; --purge-data removes data too
```

`status` prints the service state, probes the web URL with a 2s health wait, then
the boot check: encryption, automatic login, session presence, and a one-line
posture verdict.

`uninstall` with `--purge-data` and no `--yes` prompts before deleting `data/`
and `secrets/`; without `--purge-data` it explicitly says both were kept. It then
asks about the **system account** separately — `--remove-account` /
`--keep-account` are the non-interactive answers, and keeping is the default,
because chief's home holds its own message store and the dedicated Apple ID's
whole conversation lives there.

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

## Releases, and what `chief update` does

Upstream cuts **releases**: `chief release [major|minor|patch]` refuses a dirty
tree or a red done-check, bumps the version in `pyproject.toml`, commits, tags
`vX.Y.Z`, pushes branch and tag, and publishes a GitHub Release whose notes are
the subject lines since the previous tag. Boxes consume releases; never run this
on one.

A box runs *a release plus everything it has since edited about itself*. So an
update never checks anything out — it applies the box's **local layer onto** the
release, keeping chief's edits as the side that wins and adapting them to
upstream's structure.

1. `git fetch --force --tags origin`; failure returns 1.
2. Resolve the newest `vX.Y.Z` tag — **numerically**, so `v0.10.0` beats `v0.9.0`.
3. Read the **base pin**, `refs/chief/base`: the release commit this box last
   synced to. It is a *local* ref, which is what makes update survive an upstream
   history rewrite and keeps the base commit alive against gc. No pin returns 1
   with instructions rather than guessing.
4. Pin equals the newest release → "already up to date", return 0.
5. **Dirty guard** on tracked files only; untracked instance data (`skills/`,
   `config.yaml`, `secrets/`, `data/`) never counts.
6. `git merge-tree --write-tree --merge-base=<pin> HEAD <tag>` — a real
   three-way merge with the pin supplied *explicitly*, so git is never asked for
   a common ancestor and unrelated histories still merge.
7. `git read-tree -u --reset <merged tree>` — index and working tree get the
   result, **HEAD stays on the pre-update commit**. That is what leaves the
   update as ordinary uncommitted changes, so the self-edit seatbelt applies
   verbatim and needs no new knobs.
8. Write `data/update_pending.json` (target commit, version, pre-update HEAD).
9. Print `clean:` or `conflicts:` with the file list, and stop. Exit `0` clean,
   `2` conflicted, `1` could not run.

Chief takes it from there via the **`self-update` skill**: resolve any conflict
markers, then `restart` — full done-check, one `update to vX.Y.Z` commit, reboot,
and the existing boot-failure rollback. Three consecutive red checks trip the
existing circuit breaker; the correct give-up is **`chief update --abort`**,
which undoes the applied update and forgets the pending record, putting the box
back exactly on the version it was already running.

**The base pin advances only after a healthy boot**, and only if HEAD actually
moved — proof the update was committed. A rollback or a red check leaves HEAD
where it was, so the pending record is discarded and the pin does not move. The
give-up must use `--abort`, not a bare `revert_edits`: the latter reverts the
tree but leaves the pending record and HEAD unmoved, and a *later* unrelated
commit's healthy boot would then be misread as this update landing. The next
update retries from the same place.

### Local layer = tree diff, not a commit range

Deliberate, and load-bearing. An update commits as a *single squashed* commit, so
HEAD's parent is the previous box commit rather than the release it came from.
After one update, `rebase --onto` or any commit range can no longer tell the
local layer from all of history. Tracking a pinned base and merging trees is
behaviourally the rebase, mechanically not one. Don't "simplify" it back.

### Installed skills are instance data

`skills/` is untracked, like `config.yaml`, `secrets/`, and `data/`. Chief edits
its own installed skills, and tracking them made every release collide with that
— the old handling gave up and printed "reconcile by hand". Tracked *sources*
live in `core-skills/` (core's own) and `packages/*/skills/` (packages'); boot
seeds a core skill into `skills/` only when it is missing, never over an existing
copy. A skill genuinely changed on both sides is now just another conflicted file
for chief to reconcile.

### Autonomy and the schedule

`update.autonomy` — `off` (manual only), `clean-only` (default: apply clean
updates, ask the owner on a collision), `full` (resolve unattended and report
afterwards). It governs *scheduled* runs; the owner asking chief directly always
works.

`update.schedule` is a cron spec (UTC) the installer offers to set. Boot creates
a matching **prompt** schedule — chief woken as a normal turn, so quiet hours and
safe-boundary restarts come for free. Config is the source of truth: chief can
retune or delete the schedule, but clearing the key is what stops it coming back.

### Crossing over from the merge-based update (one time)

An existing box takes one final old-style `chief update`, then the new code's
boot migration runs, idempotently: untrack `skills/` (one
`chore: untrack installed skills` commit, working-tree copies kept), seed any
missing core skill from `core-skills/`, and record the base pin at the newest
release contained in HEAD (falling back to the shared ancestor; if there is
neither it logs loudly and pins nothing rather than inventing a base). If a
self-edited core skill was clobbered by that final merge, its pre-migration copy
is still in git history.

Cloned packages update separately via `chief-pkg update`, which restarts nothing.

### There are no migrations — new columns are a manual deploy step

The schema is created at boot, so an update that adds a column leaves an existing
`data/chief.db` without it, and every read of that table raises `no such column`.
Run the `ALTER TABLE` by hand at deploy; each such commit says which one in its
message.

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

## Migrating a live box to the dedicated account

A human runs this, once, on one machine. There is no `chief migrate` command and
there should not be — every step below wants eyes on it. Write the abort path
down **before** starting; it is the last section here.

> **This checklist has never been run end to end on real hardware.** It is
> written from the code, not from a completed migration. The box it was written
> for never got past step 4 — Apple declined to issue the second Apple ID, and
> without one the dedicated posture is impossible, since it rests on one iMessage
> account per user session.
>
> Two things follow. First, treat the abort path as the load-bearing part: have
> it written down and the backup taken before step 6, because you are the one
> finding the bugs. Second, the interactive path this checklist drives has the
> thinnest test coverage in the installer — `installer-e2e` runs
> `--non-interactive`, which skips the whole dedicated branch, and every one of
> the three high-severity findings in the review of #288 lived in exactly that
> gap. Known-open holes at the time of writing: #289 (`grant_reason` accepts
> paths it should not), #290 (a group chat both accounts are in delivers twice),
> #291 (uninstall offers to delete an account it never created).
>
> What *is* verified on real macOS (26.5.2): `sysadminctl -addUser … -password -`
> reads the password from piped stdin, so the create step neither hangs nor
> leaks the password onto argv. Ignore the `No clear text password or
> interactive option was specified` line it prints — it appears on success too.
>
> If you run this and it works, delete this notice. If it does not, please file
> an issue with the step number.

**Before you touch anything**

1. Take a full backup of the tree (`data/`, `secrets/`, `config.yaml`, and the
   git history — this box's lineage carries its own self-edit commits and is not
   recoverable from upstream).
2. Note the current service definition's path and contents, and where the tree
   lives now. That pair is what the abort path restores.
3. `chief status` and record it. This is the "known good" you are comparing to.
4. Create chief's Apple ID (email-only) but do **not** sign in yet. There is no
   CLI or API for this — it is browser-only, and Apple can simply refuse, which
   is where this checklist's own trial run ended. Do this step *first*: if you
   cannot get a second Apple ID, nothing below is worth starting. Step 7 also
   asks for its password, to check chief's login password differs.
5. Check the machine's disk-encryption state — it decides the session mechanism
   and therefore whether reboots are unattended.

**The migration**

6. `chief stop`.
7. Move the tree to its permanent home if it is not there already, and run
   `chief account` from inside it (or re-run `install.sh`). Answer the two file
   grant questions deliberately; the default is none and none is usually right.
8. Confirm `secrets/` is chief-only and the tree is group-editable by you.
9. Set `imessage.owner_handles` to your handle, `imessage.self_handles` to
   chief's new one, and `imessage.owner_db_path` to your own `chat.db` if you
   want your existing monitors to keep working. `imessage.mode` is already
   `dedicated`. Set `self_handles` and `owner_db_path` **together** — the boot
   rejects one without the other, because `self_handles` is the only thing
   stopping chief from reading its own replies back out of your store.
   Reaching your store needs two grants the account plan does not make for you:
   `~/Library/Messages` is `drwx------`, so aim a read grant at it
   (`sudo chgrp -R chief ~/Library/Messages && sudo chmod -R g+rX ~/Library/Messages`),
   and macOS TCC is **per user** — chief's account needs its own Full Disk
   Access grant, given from chief's own login session in step 10
   (`packages/build-imessage/INSTALL.md` walks the same screen). Without either,
   chief logs `dropping unreadable …` and runs on its own store alone.
10. Log in as chief (console, or Screen Sharing to `vnc://127.0.0.1` on an
    encrypted disk) and sign Messages into chief's Apple ID. One iMessage
    account per user session — this is the step the whole design rests on.
11. Make sure `uv` is on chief's PATH, then let the service load, or start it by
    hand from chief's session.

**The gate — the feature is not done until all six pass, by hand**

12. The web UI is reachable at `http://127.0.0.1:8130/` from your own session
    (loopback is machine-wide).
13. A text from your phone to chief's **new** address arrives.
14. Chief's reply comes back, and does **not** poll back in as input.
15. A self-edit commit lands under chief's git identity.
16. A scheduled task fires.
17. All of the above still true after one full reboot — including whatever the
    session mechanism requires of you.

**Aborting, at any step**

1. Stop the new service (`launchctl bootout` + `pkill -9 -f chief.entrypoint` on
   macOS; plain `kickstart -k` orphans the python child).
2. Restore the tree to its original location from the backup.
3. Reinstall the previous service definition, exactly as recorded in step 2.
4. Set `imessage.mode` back to `self` and clear `owner_db_path` /
   `self_handles`.
5. Start it and confirm the old posture works — a text to yourself gets a
   prefixed reply, and `chief status` matches what you recorded in step 3.

Signing chief's Apple ID out again is optional; the old self-chat thread is
inert either way. Conversation history does not migrate — the dedicated
conversation starts fresh, by design.

### Two traps this box has already hit

- **Never `checkout` on a box.** Self-edit means every install carries local
  commits; merge only.
- **macOS 26 `chat.db` has schema drift** — no `account` table, `chat.service`
  is now `service_name`. `POLL_QUERY` touches neither, but anything you write by
  hand against that store during migration should be checked against the real
  schema, not the one you remember.
