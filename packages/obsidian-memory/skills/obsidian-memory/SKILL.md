---
name: obsidian-memory
description: Owner's Obsidian vault memory. Not a tool — recall via the `uv run chief-memory search` shell command; verbs (search/semantic/grep), conventions, CLI, ambient policy.
---

# Obsidian vault memory

The owner keeps a personal Obsidian vault. You have a hybrid (meaning +
keyword) + wikilink index over it and an ambient recall hook that surfaces
relevant notes on a cadence. Your job is to **use recall well** and **save
durable knowledge cleanly**.

## Recall — the `chief-memory` command (run it in the shell)

**There is no `obsidian-memory` / `memory` / `memory_search` native tool. Do
not call one — it does not exist and the call will fail.** Recall is a
command-line program you run through the **`shell` tool**, exactly like `ls` or
`git`. The only correct move is: call the `shell` tool with one of these command
strings (they read the index, never the model):

- `uv run chief-memory search "<query>"` — **the default; use this one.** Both
  halves of the index at once: notes that match by meaning *and* notes that
  literally contain the words, blended. Run it whenever a question might touch
  something the owner told you before (who they are, their preferences, past
  decisions, people, projects).
- `uv run chief-memory grep "<query>"` — literal matches only, ranked by how
  rare the matched words are. Reach for it when you know the exact string and
  want nothing else: a project name, a person, a filename, an error code, an
  identifier. Also the honest answer to "does the vault mention X at all?"
- `uv run chief-memory semantic "<query>"` — meaning only, ignoring wording.
  Reach for it when the owner's phrasing almost certainly differs from the
  note's: a vague description of a thing whose name you do not know.
- `uv run chief-memory related "<query>"` — `search` hits **widened by
  wikilinks**, so a linked note no query would rank still surfaces.
- `uv run chief-memory links "<note>"` — a note's 1-hop wikilink neighbourhood.
- `uv run chief-memory refresh` — force the incremental sweep now (new/changed/
  deleted notes). Rarely needed: every `search` already sweeps first, so notes
  you save show up on the next recall on their own — no command required.
- `uv run chief-memory reindex` — full rebuild; recovery only (a corrupted index
  or an edit that never bumped the file's mtime). Not part of normal use — an
  embed-model change now rebuilds on its own.

The verbs are exactly `search`, `grep`, `semantic`, `related`, `links`,
`refresh`, `reindex` — **there is no `recall` subcommand** (a bare
`chief-memory recall` errors). So to recall who the owner is, the action is:
`shell` tool → `uv run chief-memory search "owner"`. Never a tool named after
this package, and never import its Python modules — only the CLI above.

Every result line is `<score>  <absolute note path> :: <heading>`. `search`
inserts a provenance tag before the path — `[sem]` matched by meaning, `[kw]`
matched literally, `[both]` — so you can tell a hit that names your search term
outright from one that is merely nearby in meaning. The path is **absolute and
directly openable** — pass it straight to your `read_file` tool for the full
note; do not prepend a directory or `find` it. Cite the note by its path when
you use it.

If `search` returns nothing useful, try `grep` with the single most distinctive
word before concluding the vault has nothing — the blended ranking can bury a
lone literal hit under better-rounded matches.

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
