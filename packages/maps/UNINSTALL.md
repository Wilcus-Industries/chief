# Uninstalling maps

1. Delete the installed skill dir `skills/maps/` (includes the bundled
   script).
2. Deregister: `uv run python -m chief.registry_apply maps --remove`.
3. Delete this `UNINSTALL.md` (`packages/maps/UNINSTALL.md`) — its absence
   signals the uninstall completed.
4. `restart` to bring the change live.
