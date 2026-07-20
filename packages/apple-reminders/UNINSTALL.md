# Uninstalling apple-reminders

1. Delete the installed skill dir `skills/apple-reminders/`.
2. Deregister: `uv run python -m chief.registry_apply apple-reminders --remove`.
3. Delete this `UNINSTALL.md` (`packages/apple-reminders/UNINSTALL.md`) — its
   absence signals the uninstall completed.
4. `restart` to bring the change live. The owner may also revoke the
   Reminders grant in System Settings and remove the CLI with
   `brew uninstall remindctl` (leave it if they use it elsewhere).
