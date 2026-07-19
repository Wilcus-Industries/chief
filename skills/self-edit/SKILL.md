---
name: self-edit
description: Edit your own config, prompts, skills, and source safely.
---

# Editing yourself

Before editing, read `docs/ARCHITECTURE.md` — it maps where your sessions,
config, prompt, tools, commands, and packages live, with recipes for the common
self-changes. It saves you re-deriving the layout every time.

You edit your own files — config, system prompt, skills, source code, anything
on your machine — with ordinary file tools, then make the change live with one
guarded `restart`:

- `read_file` / `grep` (read-only, no approval): look before you leap. They
  reach the whole filesystem. **Always read a file before you change it.**
- `write_file` (whole-file) and `edit_file` (targeted string replace): make the
  change. These write **anywhere you have permission** — there are no carve-outs
  (you *can* write `secrets/`, `.git/`, `data/`, and off-repo paths), so the
  approval gate is the only guard; be deliberate. Edits are **inert** until you
  restart — Python does not reload live imports, so a half-finished or broken
  tree cannot hurt the running daemon.
- `restart`: with a dirty tree, the first call returns the changed-file list —
  check every file belongs to your rationale, then call again with
  `confirm=true`. It then runs the full done-check (`pytest`, `ruff`, `mypy`)
  against your working tree. **On green** it commits your edits and reboots
  into the new code (a failed boot auto-rolls-back to the last good commit).
  **On red** it *keeps your edits in place* and returns the failure so you fix
  forward and restart again. A restart with no repo changes is fine (config
  reload, script install).
- `revert_edits`: discards your uncommitted changes to tracked files (back to
  HEAD). Reach for it when the done-check keeps failing — reverting and
  rethinking beats digging deeper.

Recipe:

1. `grep`/`read_file` the files you'll touch.
2. `edit_file` (or `write_file`) each change. Prefer `edit_file` for surgical
   edits; keep one coherent change per restart so a red check is easy to read.
3. Call `restart` with a `rationale` (it becomes the commit message).
4. If the done-check comes back red, read it, fix the same files, and `restart`
   again. Don't fight the check — it is the seatbelt. After ~3 reds in a row,
   stop: show the owner the diff, or `revert_edits` and rethink.

Setting config values: use
`uv run python -m chief.config_apply dotted.key=<value>` (shell tool) — it
deep-merges deterministically. Never append blocks to `config.yaml` with the
shell (a duplicate key now fails the restart gate), and remember config.yaml
is gitignored: a bad hand-write has no rollback.

The rollback/done-check safety covers **repo files** only. Writes into `data/`,
`secrets/`, or outside the repo are gated but unversioned — no rollback. Put
secret values in files under `secrets/`, never in tracked code.

## Adding an MCP server

There is no add-server tool. An MCP server is config: `edit_file` the
`mcp_servers` config key in `config.yaml` to add an entry (a `url` for HTTP, or
a `command` argv list for stdio), then `restart`. Its tools show up as
`mcp_<name>_<tool>` after the daemon comes back up.

## Loosening or tightening your own permissions

The approval gate is config too (`gate.never` / `gate.approved` in
`config.yaml`). If the owner asks for "full permissions", add `"*"` to
`gate.approved` and `restart` — thereafter every tool auto-approves. Reverse it
by removing `"*"` and restarting. Keep gates on unless the owner says otherwise.
