# Installing apple-findmy

macOS only — stop with a clear message on any other platform. Requires
Find My.app signed into iCloud with the owner's devices/AirTags already
registered, and Homebrew. There is no Apple CLI/API for Find My — this
package works by UI automation, so two privacy grants are unavoidable.

## Steps

1. Run `bash packages/apple-findmy/install.sh` with the `shell` tool. It
   installs the `peekaboo` CLI (`brew install steipete/tap/peekaboo`,
   idempotent), copies the skill verbatim to `skills/apple-findmy/`, and
   records the install in the registry (`chief.registry_apply`). No config
   keys.
2. `restart` — the guarded commit brings the skill live.
3. Walk the owner through the grants for the daemon's host process (System
   Settings → Privacy & Security):
   - **Screen Recording** — peekaboo captures the FindMy window.
   - **Accessibility** — System Events / peekaboo clicks.
4. Verify end-to-end: open FindMy (`osascript -e 'tell application "FindMy"
   to activate'`), then `peekaboo see --app FindMy --path /tmp/findmy-ui.png`
   and confirm the printed element text names the owner's devices. Remember
   you cannot see the screenshot — the text output is your read.
5. Read the installed skill once — especially the owner's-devices-only rule
   and the AirTag foreground-refresh caveat.
