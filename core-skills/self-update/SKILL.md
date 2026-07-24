---
name: self-update
description: Update yourself onto the newest core release, keeping your own edits.
---

# Updating yourself

You run a release of core plus everything you have since edited about yourself.
An update takes the newest release and applies **your** layer onto it, so your
edits are the side that is preserved and upstream's structure is the side that
gets adapted to. Nothing is ever checked out — that would throw your edits away.

`chief update` does the deterministic git and stops. **You** do the judgment:
resolve any collisions, then make it live with `restart`, which is the same
seatbelt as any self-edit (done-check → one commit → reboot → boot-failure
rollback). There is no separate update machinery to learn.

## Doing it

1. Run the command through the shell tool, with a generous timeout — it fetches:

   ```
   chief update
   ```
   (or `uv run python -m chief.install update` if the launcher is not on PATH)

   Use `timeout=180`; the default shell timeout is far too short for a fetch.

2. Read what it says:

   - **`already up to date (vX.Y.Z)`** — stop here. Nothing to do.
   - **`clean:`** — the release is applied to your working tree. Go to step 4.
   - **`conflicts:`** — a release changed the same code you did. Step 3.
   - anything else is an error; report it and stop.

3. **Resolve each conflicted file.** Read it — the markers are `<<<<<<< HEAD`
   (your edit) / `>>>>>>> vX.Y.Z` (the release). For each one: keep the
   *intent* of your edit, take the *structure* of the release, and write the
   merged result with no markers left. If your edit was a workaround for
   something the release now fixes properly, drop the workaround — that is the
   release doing its job. If you cannot tell what an edit was for, say so to
   the owner rather than guessing.

4. `restart` with the rationale `update to vX.Y.Z`. Confirm the file list when
   it asks. On green it commits everything as that one change and reboots.

5. **On red**, fix forward against the check output and restart again — same as
   any self-edit. After three reds in a row, stop and give up cleanly:

   ```
   chief update --abort
   ```

   That undoes the applied update *and* forgets it — use it, not `revert_edits`.
   Reverting the tree alone would leave the update recorded as pending, and your
   next unrelated restart would then be misread as this update landing. Abort
   puts the box back exactly on the version it was already running. Then tell
   the owner what collided and why you could not land it. Never leave the box
   sitting on a half-merged tree.

The base pin only advances once you come back up healthy, so a rollback or a
give-up leaves you recorded as still on the old release — the next update
retries from the same place. You do not manage the pin yourself.

## When you may do this unattended

`update.autonomy` in `config.yaml`:

- **`off`** — only when the owner asks. A schedule firing does nothing but say so.
- **`clean-only`** (default) — apply and restart a clean update on your own.
  On a collision, do **not** resolve it: `chief update --abort` to put the tree
  back clean on the old version, then tell the owner what conflicted and ask.
  (Leaving the half-merged tree sitting there would wedge your next self-edit on
  the stray conflict markers.)
- **`full`** — resolve collisions yourself too, then report what you did
  afterwards, naming each file you merged and how you decided.

The owner asking you directly is always allowed regardless of this setting.

Report the outcome on the owner's primary channel when the update came from a
schedule rather than from a conversation — a scheduled run has no human on the
other end of the thread it woke.

## Cutting a release (owner's machine, not yours)

`chief release [major|minor|patch]` is the upstream side: it refuses a dirty
tree or a red done-check, bumps the version, tags `vX.Y.Z`, pushes, and
publishes a GitHub Release. Do not run it on a box — a box consumes releases,
it does not cut them.
