# Uninstalling watchers

1. Delete every schedule that runs a watcher script (`schedule` tool,
   action=list then delete).
2. Delete the installed skill dir `skills/watchers/` and the state dir
   `data/watcher-state/`.
3. Deregister: `uv run python -m chief.registry_apply watchers --remove`.
4. Delete this `UNINSTALL.md` (`packages/watchers/UNINSTALL.md`) — its
   absence signals the uninstall completed.
5. `restart` to bring the change live.
