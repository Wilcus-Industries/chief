---
name: package-manager
description: Find, install, and uninstall capability packages (document-driven).
---

# Managing packages

A package is a directory with a `manifest.yaml`, an `INSTALL.md`, an
`UNINSTALL.md`, and usually a `skills/` dir. Discovery is a small CLI you run
through the `shell` tool; install and uninstall are **document-driven** — you
follow the package's own `INSTALL.md` / `UNINSTALL.md` using your file tools
(`read_file`, `write_file`, `edit_file`), the `shell` tool, and `restart`. There
is no install/uninstall tool.

## Discover — `chief-pkg`

Run it with the `shell` tool (its cwd is the repo root):

- `chief-pkg list` — every available package (bundled + cloned).
- `chief-pkg search <query>` — match by name/description.
- add `--installed` to either to see only what's installed.
- `chief-pkg update` — pull the packages clone and report what moved.

Each line shows the name, `[installed|available]`, source (`bundled`/`cloned`),
and the **on-disk path** — read and edit the package right there. Bundled
packages live under `packages/`; cloned ones under `data/packages/` (the CLI
clones the chief-packages repo the first time it runs). Bundled wins a name
collision.

Every `chief-pkg` run pulls the clone first, so cloned packages are current
without you asking. It fails soft — on a slow or offline network you get a
warning on stderr and the previous clone, never a hang.

**`chief-pkg update` is not `chief update`.** This one moves *cloned* packages
only (data you read; nothing restarts). `chief update` moves core and the
*bundled* packages — they live in core's own git repo — and restarts the
daemon. A bundled package edit therefore needs `chief update`, not this.

## Install (follow the package's `INSTALL.md`)

1. `chief-pkg search <what the owner wants>` to find it and its path.
2. `read_file` its `manifest.yaml` (skills, config_keys, secrets) and its
   `INSTALL.md`. Gather anything the INSTALL.md marks as a **question** (handles,
   tiers, secrets) from the owner first, conversationally.
3. Do the INSTALL.md's steps. **Run the package's `install.sh`** when it has
   one — it is the deterministic path (skills, config keys, and the
   `data/installed.yaml` registry entry all land correctly); do not re-create
   its steps by hand. For `secrets`, ask the owner to place each file under
   `secrets/` themselves — never have them paste secret values into chat.
4. Config keys are ALWAYS set with
   `uv run python -m chief.config_apply dotted.key=<value>` (shell tool) — a
   deterministic deep-merge. Never hand-edit or shell-append blocks to
   `config.yaml`: a duplicated block now fails the restart gate, and
   config.yaml is gitignored so a bad hand-write is not rolled back.
5. `restart` — the guarded commit brings the skill files live; config lands by
   disk reload.
6. Verify before reporting success: `chief-pkg verify <name>` must print
   "fully installed" (it checks the registry entry, skills, config keys, and
   secrets), then confirm the capability with one real call.

## Uninstall (follow the package's `UNINSTALL.md`)

1. `chief-pkg search <name>` to find its path.
2. `read_file` its `UNINSTALL.md` and do its steps with your file tools — remove
   the installed skill dir, unset the config keys, and deregister with
   `uv run python -m chief.registry_apply <name> --remove`. Its **final step
   deletes the `UNINSTALL.md` itself**; the vanished file is the completion
   signal.
3. `restart` to bring the removal live.
