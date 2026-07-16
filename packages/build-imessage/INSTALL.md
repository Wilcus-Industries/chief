# Installing build-imessage

macOS only — stop with a clear message on any other platform. The adapter
code already ships in core (`src/chief/adapters/imessage.py`); this install
configures and enables it.

1. Copy `skills/build-imessage/SKILL.md` from this package into the core
   skills directory as `skills/build-imessage/SKILL.md` via `self_edit`.
2. Ask the owner for the handle(s) their self-chat uses (their own phone
   number and/or Apple ID email, exactly as Messages shows them, e.g.
   `+15551234567`).
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
   tell the owner to approve it. Verify by having the owner text the
   self-chat and confirming your reply (prefixed 🤖) arrives.
