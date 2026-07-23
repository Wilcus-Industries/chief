# Installing build-imessage

macOS only — stop with a clear message on any other platform. The adapter
code already ships in core (`src/chief/adapters/imessage.py`); this install
configures and enables it.

## How it works (read before installing)

The adapter supports two setups. The install **must ask the owner which one**
— do not assume; it is question 1 below. Both are handled by the same core
code; the only real difference is what goes in `owner_handles`.

- **Same-account self-DM (default, recommended)** — chief runs on the owner's
  **own** Apple ID. The owner texts their own self-chat (note-to-self); there
  is no second number, account, or device. Because Messages tags anything you
  send as "from me", these arrive with `is_from_me = 1`. The adapter delivers
  them by scoping to the self-chat: `chat.chat_identifier IN (owner_handles)`,
  which provably isolates the self-chat from every other conversation
  (owner→friend sends carry the friend's chat, so they never leak in). Here
  `owner_handles` = the owner's **own** number/email (their self-chat id).
- **Dedicated Apple ID** — chief runs on a **separate** Apple ID the owner
  texts from their own phone. Those arrive `is_from_me = 0` with the owner's
  phone as the handle, and the adapter delivers them by handle. Here
  `owner_handles` = the **owner's phone** (the number that will text the
  dedicated account). Use only if the owner wants a separate identity.

**🤖 echo guard (both modes):** chief prefixes every reply to an owner handle
with 🤖, and the adapter skips any row starting with 🤖. In self-DM mode this
is the *sole* thing stopping chief's own replies (which re-enter the store as
new `is_from_me = 1` rows) from re-dispatching in an infinite loop. It must
stay airtight.

## Steps

Gather the parameters first (steps 1–2), run the deterministic install once
(step 3), then do the interactive and customizable parts (steps 4–7).

Install does **not** ask about notify tiers or build any monitor. By default
chief texts only the owner: no non-owner message wakes it. The owner can later
ask chief to engage a specific person or group, and chief builds a monitor
scoped to that one chat then (see the build-imessage skill).

1. **Ask the owner which setup they want** (explain the two above):
   same-account self-DM (default) or a dedicated Apple ID. This decides what
   `owner_handles` means, and how you verify at the end.
2. Ask the owner for the handle(s), exactly as Messages shows them, e.g.
   `+15551234567` — for self-DM their **own** number/email (the self-chat id +
   send target); for a dedicated ID the **owner's phone** (the sender).
3. Install `screening` first (it is a prerequisite for any public-facing
   package) — follow `packages/screening/INSTALL.md`. Then place this package's
   skills and set its config, deterministically:
   - Run `IMESSAGE_HANDLES="<handle(s), comma-separated>" bash
     packages/build-imessage/install.sh` with the `shell` tool. It
     `brew install steipete/tap/imsg` (idempotent — the outbound-send CLI, see
     the imsg skill), copies **both** skills verbatim (`build-imessage` channel
     policy + `imsg` CLI usage), and sets `imessage.enabled: true` +
     `imessage.owner_handles` (via `chief.config_apply`). Homebrew is required;
     this install is macOS-only anyway.
   - **Never hand-edit `config.yaml` for these keys.** A bare handle like
     `owner_handles: +15551234567` parses as a YAML **int** and boot-loops the
     daemon (`tuple(int)` crash). If you can't run install.sh, run the same
     config write it performs — it emits a proper quoted list every time:

     ```
     uv run python -m chief.config_apply imessage.enabled=true \
       'imessage.owner_handles=["+15551234567"]'
     ```

     (For a human doing a fully manual setup: `owner_handles` must be written
     as a quoted YAML list — `["+15551234567"]` — never a bare scalar.)
   - Each package's `install.sh` records its own install in
     `data/installed.yaml` (via `chief.registry_apply`) — no hand-editing.
   - `restart` — the guarded commit brings the skill live; the config lands by
     disk reload (gitignored, not rolled back on failure) and boots
     the adapter.
4. Walk the owner through granting **Full Disk Access** to the daemon's host
   process (System Settings → Privacy & Security → Full Disk Access — add the
   terminal/launchd binary that runs chief), needed to read
   `~/Library/Messages/chat.db`.
5. First outbound send triggers a macOS **Automation → Messages** prompt; tell
   the owner to approve it (the same prompt covers `imsg send`). Verify per
   mode: for self-DM, have the owner text their **own** self-chat; for a
   dedicated ID, have them text the dedicated account from their phone. Confirm
   your reply (prefixed 🤖) arrives.
6. Verify the outbound CLI: `imsg --version` (or `command -v imsg`) with the
   `shell` tool confirms it is on PATH. With the owner's go-ahead, send one
   test text to a **non-owner** handle the owner names — `imsg send --to
   "<their handle>" --text "test from chief"` — and confirm it lands. Never
   `imsg send` to an owner handle (it bypasses the 🤖 echo guard and loops; see
   the imsg skill).
7. **Offer nightly compaction of the self-chat thread** (default: yes). The
   iMessage self-chat is long-lived — it never gets a fresh start the way a new
   web buffer does — so its transcript grows until compaction trips. A nightly
   forced compaction keeps it lean and cheap. It is the **customizable** piece,
   not baked into install.sh:
   - Explain it in one line and ask the owner to opt out if they'd rather not.
     If they decline, stop here.
   - Ask what **local** hour they want it to run (default: 3 AM — quiet, and
     compaction is a silent shell command that ignores quiet hours anyway).
   - **Convert that local hour to UTC** — cron specs are UTC (see the schedule
     tool). E.g. 3 AM in UTC−7 is `10` UTC → spec `0 10 * * *`. (This drifts
     ±1h across DST; that's fine for an overnight job. Redo it if the owner
     later cares.)
   - Create the schedule with the `schedule` tool: a **command** row (approval
     card fires — the owner is here to approve) running
     `chief compact <owner-handle>`, where `<owner-handle>` is the self-chat
     handle from step 2 (the thread_key is that bare handle). Example:
     `action=create`, `description="nightly self-chat compaction"`,
     `spec="0 10 * * *"`, `command="chief compact +15551234567"`.
   - The command talks to the running daemon over its socket and force-compacts
     the thread in-process, so the live session is folded too. Confirm the row
     with `/schedules`.
