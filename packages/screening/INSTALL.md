# Installing screening

Screening is prompt policy, not code — one skill file, nothing to configure.

1. Place the skill: copy `packages/screening/skills/screening/SKILL.md` to
   `skills/screening/SKILL.md`. Run `bash packages/screening/install.sh` with the
   `shell` tool (it does the copy), or `read_file` the source and `write_file` the
   destination yourself.
2. The script records the install in `data/installed.yaml` (via
   `chief.registry_apply`); if you copied by hand instead, run
   `uv run python -m chief.registry_apply screening` yourself.
3. `restart` — the done-check runs and the skill goes live.
4. Read the installed `skills/screening/SKILL.md` once so its rules are fresh
   this session. That's all.
