# Installing screening

Screening is prompt policy, not code — one skill file, nothing to configure.

1. Place the skill: copy `packages/screening/skills/screening/SKILL.md` to
   `skills/screening/SKILL.md`. Run `bash packages/screening/install.sh` with the
   `shell` tool (it does the copy), or `read_file` the source and `write_file` the
   destination yourself.
2. Record it: add a `screening: {source: bundled}` entry to
   `data/installed.yaml`.
3. `restart` — the done-check runs and the skill goes live.
4. Read the installed `skills/screening/SKILL.md` once so its rules are fresh
   this session. That's all.
