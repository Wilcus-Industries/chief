# Installing apple-reminders

macOS only — stop with a clear message on any other platform. Requires
Reminders.app signed into iCloud (for phone sync) and Homebrew.

## Steps

1. Run `bash packages/apple-reminders/install.sh` with the `shell` tool. It
   installs the `remindctl` CLI (`brew install steipete/tap/remindctl`,
   idempotent), copies the skill verbatim to `skills/apple-reminders/`, and
   records the install in the registry (`chief.registry_apply`). No config
   keys.
2. `restart` — the guarded commit brings the skill live.
3. Grant the Reminders permission: run `remindctl authorize` with the `shell`
   tool and tell the owner to approve the prompt on their screen; confirm
   with `remindctl status`.
4. Verify: `remindctl today --json` returns a list (empty is fine). With the
   owner's go-ahead, add and delete one test reminder end-to-end.
5. Read the installed skill once — especially the "remind me is ambiguous"
   rule (Apple Reminders vs a chief monitor) and the due-vs-alarm section.
