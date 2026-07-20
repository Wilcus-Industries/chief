---
name: apple-reminders
description: Manage Apple Reminders with the remindctl CLI (via the shell tool) — add, list, complete, and delete tasks that sync to the owner's iPhone.
---

# apple-reminders — Apple Reminders via remindctl

`remindctl` is a macOS CLI (`brew install steipete/tap/remindctl`, installed
by this package) that you drive through the **shell tool** to manage
Reminders.app. Reminders sync to the owner's iPhone/iPad via iCloud and fire
native notifications there — that reach is why they beat your own monitors
for the owner's personal to-dos.

## "Remind me" is ambiguous — clarify first

When the owner says "remind me", two different things fit:

- **Apple Reminders** (this skill): lands in Reminders.app, notifies on their
  phone, lives in their task lists.
- **A chief monitor** (the `monitor` tool): you wake up and message them.

If the ask doesn't make it obvious (e.g. "add to my groceries list" is
clearly Reminders; "ping me when the deploy finishes" is clearly a monitor),
ask once. Don't create both.

## Commands

```
remindctl                    # today's reminders
remindctl today|tomorrow|week|overdue|all
remindctl 2026-08-01         # specific date
remindctl list               # all lists
remindctl list Work          # one list
remindctl list Projects --create
remindctl add "Buy milk"
remindctl add --title "Call mom" --list Personal --due tomorrow
remindctl add --title "Meeting prep" --due "2026-08-15 09:00"
remindctl edit 87354 --due "2026-08-15 14:00"
remindctl complete 1 2 3     # complete by ID
remindctl delete 4A83 --force
remindctl today --json       # JSON for parsing (also: --plain, --quiet)
```

Dates accepted by `--due` and filters: `today`, `tomorrow`, `YYYY-MM-DD`,
`YYYY-MM-DD HH:mm`, ISO 8601.

## Due time vs alarm (early nudge)

`--due` and `--alarm` are different fields: `--due` is when the task is due,
`--alarm` is when the notification fires. For "due at 2pm, nudge me at 1:30":

```
remindctl add --title "Hairdresser" --due "2026-08-15 14:00" \
  --alarm "2026-08-15 13:30"
```

The Reminders UI may group the item under the alarm time — verify with
`remindctl today --json` (`dueDate` vs `alarmDate`) instead of assuming the
due time moved.

## Rules

- **Confirm content and due date before creating**, and before completing or
  deleting anything the owner didn't name by ID.
- **Owner-directed only** — don't rearrange their lists on your own
  initiative.
- Use `--json` when you need to parse output.
- Permission check: `remindctl status`; request with `remindctl authorize`.
  A "not authorized" error means the Reminders permission prompt was denied —
  ask the owner to approve it in System Settings → Privacy & Security.
- If `remindctl` is missing (`command not found`), this package isn't fully
  installed — say so; do not fall back to hand-written AppleScript.
