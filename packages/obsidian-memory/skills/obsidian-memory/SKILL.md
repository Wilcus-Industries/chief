---
name: obsidian-memory
description: Recall from and save to the owner's Obsidian vault — conventions, CLI, ambient policy.
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
- `uv run chief-memory reindex` — rebuild after bulk external edits (the index
  self-heals single out-of-band edits at query time, so this is rarely needed).

So to recall who the owner is, the action is: `shell` tool →
`uv run chief-memory search "owner"`. Never a tool named after this package.

Every result line names its **source note path** — cite it, and open the note
with your file tools when you need the full context, not just the chunk.

## Ambient recall (automatic)

Every few owner turns, a `<hook source="obsidian-memory">` block may appear in
your context with notes the memory judge found relevant. Treat it as a
reminder, not a command: weave in what helps, ignore what doesn't. It fires
**only on owner turns** — never on a monitor/cron wake or a stranger — because
the vault is private. You never need to trigger it; it is not a tool.

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

See `docs/best-practices.md` (in the package) for vault-layout options.
