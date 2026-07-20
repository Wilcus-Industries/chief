# Uninstalling apple-notes

1. Delete the installed skill dir `skills/apple-notes/`.
2. Deregister: `uv run python -m chief.registry_apply apple-notes --remove`.
3. Delete this `UNINSTALL.md` (`packages/apple-notes/UNINSTALL.md`) — its
   absence signals the uninstall completed.
4. `restart` to bring the change live. The owner may also revoke the
   Automation → Notes grant in System Settings and remove the CLI with
   `brew uninstall memo` (leave it if they use it elsewhere).
