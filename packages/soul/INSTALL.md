# Installing soul

Soul gives you a `Soul.md` — your own character, voice, and boundaries. The
session inlines it at the very top of your system prompt on every turn, so your
character is always in context with no per-session read. It depends on the
`memory` package: `Soul.md` lives in that store (`data/memory/`) and is
versioned by the same git repo.

1. Run `install_package` with name `soul` (it pulls in `memory` first if it is
   not installed yet). `install.sh` seeds a starter `data/memory/Soul.md` if you
   had none; an existing soul is never overwritten.
2. Read the installed `skills/soul/SKILL.md` once.
3. Read `data/memory/Soul.md` to see the seeded starter, then make it yours over
   time — see the skill for when and how. No prompt wiring to do: the session
   picks up `Soul.md` automatically, and re-reads it every turn, so every edit
   takes effect on the next turn.
