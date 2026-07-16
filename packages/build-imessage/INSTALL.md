# Installing build-imessage

macOS only — stop with a clear message on any other platform. The adapter
code already ships in core (`src/chief/adapters/imessage.py`); this install
configures and enables it.

## What this is: same-account self-DM

Chief runs on the owner's **own** Apple ID. The owner texts their own
self-chat (the note-to-self conversation with their own number), and chief
answers there. There is **no second number, no second Apple ID, no separate
device** — the assistant lives inside the owner's own Messages.

The load-bearing consequence: messages the owner sends to their self-chat
arrive in `chat.db` with `is_from_me = 1` (Messages tags anything you send as
"from me", even to yourself). So the adapter must dispatch the owner's own
`is_from_me = 1` self-chat rows — it scopes to the self-chat by
`chat.chat_identifier IN (owner_handles)`, which provably isolates the
self-chat from every other conversation (owner→friend sent messages carry the
friend's handle and a different `chat_identifier`, so they never leak in).

Two guards keep this safe:
- **Scope** — only rows in the self-chat (`chat_identifier ∈ owner_handles`)
  are read. Other conversations are invisible.
- **🤖 echo guard** — chief prefixes every self-chat reply with 🤖, and the
  adapter skips any `is_from_me = 1` row starting with 🤖. This is the sole
  thing preventing chief's own replies (which re-enter the store as new rows)
  from re-dispatching in an infinite self-reply loop. It must stay airtight.

## Steps

1. Copy `skills/build-imessage/SKILL.md` from this package into the core
   skills directory as `skills/build-imessage/SKILL.md` via `self_edit`.
2. Ask the owner for the handle(s) their self-chat uses — their own phone
   number and/or Apple ID email, exactly as Messages shows them, e.g.
   `+15551234567`. This value is both the self-chat's `chat_identifier` (what
   scopes inbound) and the send target.
3. Ask the owner to pick a notify tier (explain the trade-offs, see the
   build-imessage skill):
   - **notify-all** — every third-party text wakes you (most expensive);
   - **notify-whitelist** — only listed handles wake you, the rest are logged;
   - **no-notify** — nothing wakes you; you read on demand or via your own
     monitors.
   On notify-all, offer the optional cheap-model screen ("worth waking?").
4. Walk the owner through granting **Full Disk Access** to the daemon's host
   process (System Settings → Privacy & Security → Full Disk Access — add the
   terminal/launchd binary that runs chief), needed to read
   `~/Library/Messages/chat.db`.
5. Set the config via `self_edit` on `config.yaml`:

   ```yaml
   imessage:
     enabled: true
     owner_handles: ["+15551234567"]
   ```

   The self-edit restart loads the adapter.
6. Create the tier's monitor (see the build-imessage skill for the exact
   predicate shapes). No monitor for no-notify.
7. First outbound send triggers a macOS **Automation → Messages** prompt;
   tell the owner to approve it. Verify by having the owner text their own
   self-chat and confirming your reply (prefixed 🤖) arrives.
