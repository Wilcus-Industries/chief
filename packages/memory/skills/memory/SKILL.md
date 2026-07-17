---
name: memory
description: Recall before acting; save durable facts; keep the MEMORY.md index current.
---

# Memory

You have a persistent, git-backed markdown store at `data/memory/`. It is
yours across every conversation — the thread resets, this does not. Use it so
the owner never has to tell you the same thing twice.

## Shape

- `data/memory/MEMORY.md` — the index. One line per fact file. It loads every
  session; the fact files do **not**. This is your table of contents.
- `data/memory/facts/<slug>.md` — one fact per file. Atomic, so it can be
  found, updated, or deleted on its own.

Each fact file carries frontmatter, then the fact:

```markdown
---
name: <short-kebab-slug>
description: <one-line summary — used to judge relevance on recall>
type: owner | feedback | project | reference
---

<the fact. For feedback/project, follow with **Why:** and **How to apply:**
lines. Link related facts with [[their-slug]].>
```

Types: `owner` — who the owner is (role, preferences, people, standing
context). `feedback` — how the owner wants you to work; corrections and
confirmed habits, always with the why. `project` — ongoing work, goals, or
constraints not derivable from the tools; write absolute dates, not "next
week". `reference` — pointers to external resources (URLs, accounts, IDs).

## Recall — before you act

When a request touches anything you might already know (the owner, an ongoing
project, a past preference), scan `MEMORY.md` first and open the fact files
whose hooks look relevant. Do this before asking the owner a question they may
have already answered.

Recalled facts describe what was true when written. If one names a file, an
account, or a setting, confirm it still holds before relying on it.

## Save — as you learn

Save a fact the moment you learn something durable that a future you would
otherwise have to re-ask or re-derive. Do not wait to be told "remember this".

1. Check `MEMORY.md` for a file that already covers it. If one exists, update
   that file — do not create a near-duplicate.
2. Otherwise write `facts/<slug>.md` with the frontmatter above.
3. Add or update its one-line pointer in `MEMORY.md`:
   `- [Title](facts/<slug>.md) — hook`.
4. Commit the store: `git -C data/memory add -A && git -C data/memory commit
   -m "memory: <what changed>"`. Every change is a commit — that is the undo.

Do **not** save: secrets or credentials (those live in `secrets/`, never
here); things the tools already tell you (current config, file contents, live
state); or details that only matter to the conversation in front of you. If
asked to "remember" one of those, save what was *non-obvious* about it, not the
raw value.

## Prune

A wrong memory is worse than none. When a fact turns out stale or false, fix
it or delete the file and drop its `MEMORY.md` line — in the same commit. Keep
the index tight; fewer sharp facts beat an accreting pile.
