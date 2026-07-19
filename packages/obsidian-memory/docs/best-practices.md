# Vault layout best practices

Reference for the install interview (INSTALL.md step 3). Pick one layout with
the owner; each indexes fine — the difference is how the owner thinks. Recall
chunks by heading and traverses `[[wikilinks]]`, so whatever the layout, favor
clear headings and generous linking.

## PARA (Projects, Areas, Resources, Archive)

Four top-level folders by actionability: `projects/` (active, goal-bound),
`areas/` (ongoing responsibilities), `resources/` (reference topics),
`archive/` (inactive). Good default for a mixed work/life vault. New notes land
in the folder matching their current role and move to `archive/` when done.

## Zettelkasten (atomic notes)

Many small, single-idea notes in a flat space, each densely `[[linked]]` to
related notes. No deep folders; structure emerges from links. Strong fit for
this package because `related` and `links` traverse exactly those connections.

## MOC (Maps of Content)

Atomic notes plus curated "index" notes (Maps of Content) that link out to a
topic's notes — a hand-built table of contents per area. Combine with PARA or
Zettelkasten. The MOC notes themselves make excellent `links` starting points.

## Daily notes

One note per day (`daily/YYYY-MM-DD.md`) as a capture stream, with durable
ideas promoted into their own linked notes over time. Simple to maintain; pair
with any of the above so the daily log doesn't become the only structure.

## Whatever the layout

- Exclude `.obsidian/` (app state), `templates/`, and `attachments/` from the
  index (the defaults do this) — plus any private folder the owner names.
- Sync with **git** if possible: versioned, diffable, and chief can commit.
- Keep one idea per heading; that is the unit recall retrieves.
