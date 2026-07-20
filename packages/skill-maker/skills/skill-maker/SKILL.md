---
name: skill-maker
description: Author a new skill or package when a workflow proves worth keeping — where files go, what the validators require, and the writing-quality rules that make a skill actually change behavior.
---

# skill-maker — crystallizing workflows into skills

When the owner says "remember how to do this" about a *procedure* (not a
fact — facts go to memory), or you've done the same multi-step dance three
times, write it down as a skill. A skill is procedural memory: it must
change your future behavior, or it's clutter.

## Where things go

- **Standalone skill** (prompt-only, no installs/config):
  `skills/<name>/SKILL.md` via a guarded self-edit. Live after `restart`.
- **Package** (needs a CLI, Python deps, config keys, or scripts):
  `packages/<name>/` with `manifest.yaml`, `skills/<name>/SKILL.md`,
  `install.sh`, `INSTALL.md`, `UNINSTALL.md`. Copy the shape of an existing
  simple package (`packages/screening/` is the minimal one, `packages/maps/`
  bundles a script, `packages/whisper/` installs a brew CLI).

## What the validators require (done-check enforced)

- SKILL.md: frontmatter opening `---` on line 1, closed `---`, non-empty
  `name` + `description`, non-empty body. A bogus write fails pytest and
  rolls back.
- manifest.yaml: parses as a mapping with non-empty `name` + `description`;
  a `hooks:` block needs `module` + `register`.
- Prose files wrap at 88 columns like everything else in the repo.

## Writing rules — what makes a skill good

1. **Every line must change behavior.** "Be careful" is a no-op; delete it.
   State the exact command, the exact rule, the exact failure signature.
2. **Description = trigger.** The description is what future-you reads when
   deciding to open the skill. Name the situation it serves, not a summary
   of the body.
3. **Steps end with completion criteria.** "Verify: X returns Y" beats
   "make sure it works".
4. **Co-locate rule with concept.** One idea, one place; don't scatter a
   rule across sections that will drift apart.
5. **Skills get sharper, not longer.** When adding a rule, delete the
   wording it replaces. If a skill grows past ~90 lines, it's probably two
   skills or carrying sediment.
6. **Name the pitfalls you actually hit** — the wrong command you tried,
   the flag that bit you. That's the highest-value content a skill has.
7. **Don't duplicate a neighbor.** Read the existing `skills/` dir first;
   extend the closest skill rather than adding a narrow sibling.

## Workflow

1. Confirm with the owner it's worth keeping (skip when they explicitly
   asked for a skill).
2. Check `skills/` for overlap — extend before creating.
3. Write the file(s) with your file tools; standalone skills straight into
   `skills/`, packages under `packages/` + run their install.sh.
4. `restart` — the done-check validates and the guarded commit lands it.
   The new skill is only in your prompt after the restart.
5. Next time the workflow comes up, follow the skill — and fix it where it
   proves wrong. A skill nobody corrects goes stale.
