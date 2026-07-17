# Installing memory

Memory is a discipline over your own file and git tools, not new code. The
`install_package` tool copies its skill verbatim and seeds an empty store (via
this package's `install.sh`) — you do not retype anything.

1. Run `install_package` with name `memory`.
2. Read the installed `skills/memory/SKILL.md` once so the recall/save/index
   rules are fresh in this session.
3. The store lives at `data/memory/` (git-backed, gitignored from the harness
   repo). `install.sh` created it with a `MEMORY.md` index and a `facts/`
   directory if they were not already there — an existing store is never
   overwritten.

That is all there is to configure. From here on, follow the skill: recall
before you act, save durable facts as you learn them, and keep `MEMORY.md`
pointing at them.
