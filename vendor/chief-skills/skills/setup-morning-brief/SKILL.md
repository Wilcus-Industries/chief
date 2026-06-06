---
name: setup-morning-brief
description: "Use this skill when the owner wants to set up, change, or turn off a recurring morning brief (a.k.a. daily digest, daily briefing, morning update, or morning routine) — a message that arrives at the same time every day summarizing what's ahead. Triggers include any request like 'send me a morning brief', 'give me a daily digest at 7am', 'start my morning update', or 'stop my daily briefing'. This skill runs a short interview, then registers the brief as a recurring scheduled wakeup; it does NOT itself write the brief each morning — the scheduled wakeup does that later."
---

# Set up a recurring morning brief

A morning brief is a recurring **wakeup** schedule: every day at a fixed local time the
scheduler boots a full agent turn whose prompt tells you (chief) to assemble and send the
brief using whatever tools you have then (calendar, inbox, tasks, web). Your job in this
skill is only the **setup**: interview the owner, then register that schedule once. You do
not draft the brief now.

## How it works

The owner's schedule tools already exist — you reach the brief through
`mcp__chief_schedule__schedule_recurring`:

- `cron`: a 5-field cron expression, read in the **owner's local time** (you do not handle
  timezones — a bare local time is correct).
- `action_type`: must be **`"wakeup"`** (not the default `"message"`) — the brief needs a
  real agent turn to gather and write content, not a fixed string.
- `action`: the **wakeup prompt** — the standing instruction for each morning's turn (see
  below). Write it as a directive to your future self.
- `thread_key` (optional): where it lands; omit to use the owner's primary inbox.
- `urgent` (optional): set `true` only if the chosen time can fall inside quiet hours and
  the owner still wants it then.

## Steps

1. **Interview — keep it to one or two messages.** Confirm:
   - **Time**: what local time each day (e.g. 7:00am). Convert to a daily cron, e.g.
     `0 7 * * *`. For weekdays-only use `0 7 * * 1-5`.
   - **Contents**: what to include. Offer sensible defaults and let the owner trim/add —
     today's calendar, anything due or overdue, unread/important inbox items, weather, and
     a short heads-up on anything time-sensitive. Capture their picks concretely.
   - **Delivery**: default to the primary inbox unless they name another conversation.

   If the owner already gave enough ("7am, calendar + tasks + weather"), skip straight to
   confirming the cron and contents — don't re-ask what you already know.

2. **Write the wakeup prompt** (the `action`). Make it self-contained — the morning turn
   has no memory of this interview. Name the sections the owner chose and the order, e.g.:

   > Assemble and send the owner's morning brief. Use your tools to pull: (1) today's
   > calendar, (2) tasks due or overdue, (3) important unread mail, (4) today's weather.
   > Lead with anything time-sensitive. Keep it skimmable — short headers, no preamble. If
   > a source is unavailable, note it briefly and continue rather than failing the brief.

   Tailor that list to exactly what the owner picked. Do not invent sections they declined.

3. **Register it.** Call `mcp__chief_schedule__schedule_recurring` with the `cron`,
   `action_type="wakeup"`, the `action` prompt, and `thread_key`/`urgent` only if needed.

4. **Confirm.** Report the schedule id and next fire time from the tool result, and tell
   the owner they can change cadence or contents by editing it (re-run this skill) or stop
   it with `cancel_schedule` using that id. To see all active schedules, `list_schedules`.

## Changing or stopping an existing brief

- **Change**: there's no in-place edit — cancel the old schedule (`cancel_schedule` with
  the id from `list_schedules`) and register a fresh one. Carry over the parts the owner
  is keeping so they don't have to restate them.
- **Stop**: find the brief in `list_schedules` and `cancel_schedule` its id. Confirm it's
  off.
