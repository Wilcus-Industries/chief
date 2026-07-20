# Uninstalling skill-maker

1. Delete the installed skill dir `skills/skill-maker/`.
2. Deregister: `uv run python -m chief.registry_apply skill-maker --remove`.
3. Delete this `UNINSTALL.md` (`packages/skill-maker/UNINSTALL.md`) — its
   absence signals the uninstall completed.
4. `restart` to bring the change live.
