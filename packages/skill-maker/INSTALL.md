# Installing skill-maker

Works anywhere — prompt policy only, no dependencies or config.

## Steps

1. Run `bash packages/skill-maker/install.sh` with the `shell` tool. It
   copies the skill verbatim to `skills/skill-maker/` and records the
   install in the registry (`chief.registry_apply`).
2. `restart` — the guarded commit brings the skill live.
3. Read the installed skill once; from then on it governs how you turn
   recurring workflows into skills and packages.
