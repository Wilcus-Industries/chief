---
name: self-edit
description: Edit your own config, prompts, skills, and source safely.
---

# Editing yourself

Before editing, read `docs/ARCHITECTURE.md` — it maps where your sessions,
config, prompt, tools, commands, and packages live, with recipes for the common
self-changes. It saves you re-deriving the layout every time.

You can change your own files — config, system prompt, skills, and source code —
with two tools:

- `read_file` / `grep` (read-only, no approval): look before you leap. **Always
  read a file before you rewrite it** — `self_edit` replaces whole files, so
  editing blind loses everything you didn't retype.
- `self_edit`: hand it `files` (a map of repo-relative path → complete new
  contents) plus a `rationale`. It runs the guarded pipeline: your edit lands on
  a scratch branch, the full done-check runs there (`pytest`, `ruff`, `mypy`),
  and **only a green check merges and restarts** the daemon into the new code. A
  failed boot auto-rolls-back to the last good commit. A red check is reverted
  and the failure comes back to you to fix.

Recipe:

1. `grep` for the symbol or setting; `read_file` each file you'll touch.
2. Call `self_edit` with the **entire** new contents of every changed file. Keep
   edits small — one coherent change per call is easier to get green.
3. If the done-check fails, read the output, fix, and retry. Don't fight it —
   the check is the seatbelt.

`secrets/`, `.git/`, and `data/` are off-limits to `self_edit` (and reads can't
touch `secrets/` or `.git/`). Put secret values in files under `secrets/`, never
in tracked code.

## Adding an MCP server

There is no add-server tool. An MCP server is config: `self_edit` the
`mcp_servers` config key to add an entry (a `url` for HTTP, or a `command` argv
list for stdio), then the restart connects it. Its tools show up as
`mcp_<name>_<tool>` after the daemon comes back up.
