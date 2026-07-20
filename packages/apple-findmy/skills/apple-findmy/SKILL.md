---
name: apple-findmy
description: Locate the owner's Apple devices and AirTags by driving FindMy.app with peekaboo UI automation and AppleScript (via the shell tool).
---

# apple-findmy — device/AirTag locations from FindMy.app

Apple ships **no CLI or API** for Find My, so this skill drives the app's UI:
open FindMy.app with AppleScript, then read its window with `peekaboo`
(`brew install steipete/tap/peekaboo`, installed by this package). Everything
runs through the **shell tool**.

**You cannot see screenshots.** Prefer peekaboo's *text* output — its UI
element list carries device names and location strings you can read directly.
Save a screenshot only as a deliverable for the owner, never as something to
"look at" yourself.

## When to use

- Owner asks "where is my iPhone / my keys / the cat's AirTag?"
- Periodic location checks of an AirTag'd item (via a monitor).

## Procedure

```
# 1. Open Find My and let it load
osascript -e 'tell application "FindMy" to activate'
sleep 3

# 2. Read the window as text — element labels include names + locations
peekaboo see --app FindMy --annotate --path /tmp/findmy-ui.png
```

`peekaboo see` prints the detected UI elements (IDs + text) alongside the
annotated screenshot; read the printed element text for locations. To open an
item's detail view, click it by element ID and re-read:

```
peekaboo click --on B3 --app FindMy
peekaboo see --app FindMy --path /tmp/findmy-detail.png
```

Tabs: `Devices` (iPhone/iPad/Mac/AirPods) vs `Items` (AirTags) — click the
toolbar button by its element ID, or via System Events:

```
osascript -e 'tell application "System Events" to tell process "FindMy" to \
  click button "Items" of toolbar 1 of window 1'
```

If a location string doesn't surface in the element text, fall back to
sending the owner the screenshot file and saying what you could not read.

## Rules

- **Owner's devices only.** Never locate a device or tag the owner doesn't
  own — refuse tracking of other people outright.
- AirTags update **only while the FindMy page is open and frontmost** — keep
  it foregrounded during a reading; for tracking over time, use the `monitor`
  tool to re-run the procedure periodically and log locations.
- UI automation is brittle across macOS versions: if a scripted click
  errors, re-run `peekaboo see` and adapt to the current element IDs; check
  `peekaboo --help` before trusting exact flags.
- Permissions: peekaboo needs Screen Recording; System Events clicks need
  Accessibility (System Settings → Privacy & Security). "Not authorized"
  errors mean a grant is missing — ask the owner.
- If `peekaboo` is missing (`command not found`), this package isn't fully
  installed — say so before improvising raw AppleScript.
