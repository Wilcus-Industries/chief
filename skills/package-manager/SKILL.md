---
name: package-manager
description: Find, install, and uninstall capability packages (document-driven).
---

# Managing packages

A package is a directory with a `manifest.yaml`, an `INSTALL.md`, an
`UNINSTALL.md`, and usually a `skills/` dir. Discovery is a small Bash CLI;
install and uninstall are **document-driven** — you follow the package's own
`INSTALL.md` / `UNINSTALL.md` using your file tools (`read_file`, `write_file`,
`edit_file`) and `restart`. There is no install/uninstall tool.

## Discover — `chief-pkg`

Run it via Bash:

- `chief-pkg list` — every available package (bundled + cloned).
- `chief-pkg search <query>` — match by name/description.
- add `--installed` to either to see only what's installed.

Each line shows the name, `[installed|available]`, source (`bundled`/`cloned`),
and the **on-disk path** — read and edit the package right there. Bundled
packages live under `packages/`; cloned ones under `data/packages/` (the CLI
clones the chief-packages repo the first time it runs). Bundled wins a name
collision.

## Install (follow the package's `INSTALL.md`)

1. `chief-pkg search <what the owner wants>` to find it and its path.
2. `read_file` its `manifest.yaml` (skills, config_keys, secrets) and its
   `INSTALL.md`. Gather anything the INSTALL.md marks as a **question** (handles,
   tiers, secrets) from the owner first, conversationally.
3. Do the INSTALL.md's steps with your file tools: `write_file`/`edit_file` to
   place skill files and set config keys, run any build script it names via
   Bash. For `secrets`, ask the owner to place each file under `secrets/`
   themselves — never have them paste secret values into chat.
4. Record the install in `data/installed.yaml` (`write_file`/`edit_file`): a
   `<name>: {source: bundled|cloned, commit: <origin commit>}` entry. `chief-pkg
   --installed` reads this.
5. `restart` — one guarded commit brings the skill files and config live.
6. Confirm the capability works with a real call before reporting success.

## Uninstall (follow the package's `UNINSTALL.md`)

1. `chief-pkg search <name>` to find its path.
2. `read_file` its `UNINSTALL.md` and do its steps with your file tools — remove
   the installed skill dir, unset the config keys, drop its `data/installed.yaml`
   entry. Its **final step deletes the `UNINSTALL.md` itself**; the vanished
   file is the completion signal.
3. `restart` to bring the removal live.
