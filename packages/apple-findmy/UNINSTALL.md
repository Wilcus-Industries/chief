# Uninstalling apple-findmy

1. Delete the installed skill dir `skills/apple-findmy/`.
2. Deregister: `uv run python -m chief.registry_apply apple-findmy --remove`.
3. Remove any location-tracking monitor you created for it (`monitor` tool).
4. Delete this `UNINSTALL.md` (`packages/apple-findmy/UNINSTALL.md`) — its
   absence signals the uninstall completed.
5. `restart` to bring the change live. The owner may also revoke the Screen
   Recording / Accessibility grants in System Settings and remove the CLI
   with `brew uninstall peekaboo` (leave it if they use it elsewhere).
