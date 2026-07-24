---
name: obsidian-memory
description: Owner's Obsidian vault memory. Not a tool — recall via the `uv run chief-memory search` shell command; conventions, CLI, ambient policy.
---

# Obsidian vault memory

The owner keeps a personal Obsidian vault. You have a semantic + wikilink index
over it and an ambient recall hook that surfaces relevant notes on a cadence.
Your job is to **use recall well** and **save durable knowledge cleanly**.

## Recall — the `chief-memory` command (run it in the shell)

**There is no `obsidian-memory` / `memory` / `memory_search` native tool. Do
not call one — it does not exist and the call will fail.** Recall is a
command-line program you run through the **`shell` tool**, exactly like `ls` or
`git`. The only correct move is: call the `shell` tool with one of these command
strings (they read the index, never the model):

- `uv run chief-memory search "<query>"` — semantic hits, one note path per
  line. Run it whenever a question might touch something the owner told you
  before (who they are, their preferences, past decisions, people, projects).
- `uv run chief-memory related "<query>"` — semantic hits **widened by
  wikilinks**, so a linked note a pure search would miss still surfaces.
- `uv run chief-memory links "<note>"` — a note's 1-hop wikilink neighbourhood.
- `uv run chief-memory refresh` — force the incremental sweep now (new/changed/
  deleted notes). Rarely needed: every `search` already sweeps first, so notes
  you save show up on the next recall on their own — no command required.
- `uv run chief-memory reindex` — full rebuild; recovery only (a corrupted index,
  an embed-model change, or an edit that never bumped the file's mtime). Not part
  of normal use.

The verbs are exactly `search`, `related`, `links`, `refresh`, `reindex` —
**there is no `recall` subcommand** (a bare `chief-memory recall` errors). So to
recall who
the owner is, the action is: `shell` tool → `uv run chief-memory search "owner"`.
Never a tool named after this package, and never import its Python modules —
only the CLI above.

Every result line is `<score>  <absolute note path> :: <heading>`. The path is
**absolute and directly openable** — pass it straight to your `read_file` tool
for the full note; do not prepend a directory or `find` it. Cite the note by its
path when you use it.

## Ambient recall (automatic)

Every few owner turns, a `<hook source="obsidian-memory">` block may appear in
your context listing a few notes that passed the `memory-relevance` gate.

Those are **pointers, not content** — an absolute path and a heading, nothing
more. If a note looks like it would help, open the path directly with `read_file`;
if it doesn't, ignore it and say nothing. Never claim to know what a note says on
the strength of its heading alone: you have not read it yet.

The block fires **only on owner turns** — never on a monitor/cron wake or a
stranger — because the vault is private. You never need to trigger it; it is
not a tool.

## Saving — conventions (capability follows config)

You may save notes **only if** the install granted writable paths
(`obsidian_memory.writable_paths`). If it is empty, recall is **read-only** —
do not write into the vault; offer to note something elsewhere instead.

When you may write, follow Obsidian conventions so the vault stays coherent:

- One idea per note; a clear `# Title` heading (recall chunks by heading).
- Link generously with `[[Note Name]]` — links are what `related`/`links`
  traverse. Prefer linking an existing note to duplicating its content.
- Save into a granted folder (e.g. `inbox/`), never `.obsidian/`, `templates/`,
  or `attachments/` (those are excluded from the index anyway).
- Save **durable** facts (preferences, decisions, people, projects), not
  transient chatter. When in doubt, ask before writing.

See `packages/obsidian-memory/docs/best-practices.md` for vault-layout options.
