# Installing soul

Soul gives you a `Soul.md` — your own character, voice, and boundaries — and
makes you read it at the top of every conversation. It depends on the `memory`
package: `Soul.md` lives in that store (`data/memory/`) and is versioned by the
same git repo.

1. Run `install_package` with name `soul` (it pulls in `memory` first if it is
   not installed yet).
2. Read the installed `skills/soul/SKILL.md` once.
3. **Wire the pointer into your system prompt (do this now).** Open your
   editable prompt at `data/system.md` (create it if absent) and make its very
   first lines direct you to read `data/memory/Soul.md` before anything else —
   for example:

   ```
   Before you do anything, read data/memory/Soul.md. That file is who you
   are — your voice, values, and boundaries. Speak and act from it.
   ```

   This is a self-edit, so it goes through the guarded pipeline (done-check +
   rollback). Keep it at the top and keep it emphatic: the prompt is loaded
   every session, `Soul.md` is not, so this line is the only thing that pulls
   your character back in each time.
4. Read `data/memory/Soul.md`. `install.sh` seeded a starter if you had none;
   an existing soul is never overwritten. Make it yours over time — see the
   skill for when and how.
