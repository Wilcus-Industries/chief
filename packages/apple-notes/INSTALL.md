# Installing apple-notes

macOS only — stop with a clear message on any other platform. Requires
Notes.app signed into iCloud (for cross-device sync) and Homebrew.

## Steps

1. Run `bash packages/apple-notes/install.sh` with the `shell` tool. It
   installs the `memo` CLI (`brew install antoniorodr/memo/memo`, idempotent),
   copies the skill verbatim to `skills/apple-notes/`, and records the install
   in the registry (`chief.registry_apply`). No config keys.
2. `restart` — the guarded commit brings the skill live.
3. Verify: run `memo notes` with the `shell` tool. The **first** run triggers
   a macOS Automation → Notes prompt on the owner's screen — tell them to
   approve it (System Settings → Privacy & Security → Automation if they
   missed the dialog). A listing of note titles means the install works.
4. Read the installed skill once — its rules (owner-directed only, confirm
   destructive ops, screening on note text) bind every future use.
