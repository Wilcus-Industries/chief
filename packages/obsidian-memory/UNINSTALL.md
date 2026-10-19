# Uninstalling obsidian-memory

This turns off vault memory. The subpackage code stays in core
(`packages/obsidian-memory/src/`); uninstall unwires the hook, config, deps,
and skill, so it can be re-enabled later by reinstalling. It never touches the
vault itself — the owner's notes are theirs.

1. Deregister: `uv run python -m chief.registry_apply obsidian-memory --remove`, so the boot
   loader stops registering its hook.
2. Clear the config block: remove the `obsidian_memory:` key from `config.yaml`
   (`edit_file` it out, or set its sub-keys empty). No config = the hook, even
   if loaded, has no vault to read.
3. Remove the Python dependencies you added at install (reverse of INSTALL step
   6): drop `chief-obsidian-memory` from `[project.dependencies]` if you added
   it there, and run `uv sync` with the `shell` tool. Leaving it dev-only is
   fine — it just won't load in production.
4. Delete the installed skill dir `skills/obsidian-memory/`.
5. Optionally delete the persistent index at `data/hooks/obsidian-memory/` to
   reclaim disk — it is one SQLite file (plus its WAL sidecars) per vault, and
   it rebuilds itself from the notes on the first search after a reinstall. The
   **vault is never touched** — do not delete the owner's notes.
6. Delete this `UNINSTALL.md` (`packages/obsidian-memory/UNINSTALL.md`) — its
   absence signals the uninstall completed.
7. `restart` to bring the change live.
