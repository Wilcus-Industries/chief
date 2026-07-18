# Uninstalling build-imessage

This disables the iMessage channel. The adapter code stays in core
(`src/chief/adapters/imessage.py`) — uninstall only unwires config and the
skill, so it can be re-enabled later by reinstalling.

1. Turn the channel off in `config.yaml`: set `imessage.enabled: false` (leave
   `owner_handles` or clear it — your call). `edit_file` the key, or run
   `python -m chief.config_apply imessage.enabled=false` via Bash.
2. Delete the installed skill dir `skills/build-imessage/`.
3. Remove the `build-imessage` entry from `data/installed.yaml`. (Leave
   `screening` unless the owner also wants it gone — see its `UNINSTALL.md`.)
4. Remove the notify-tier monitor you created for iMessage, if any (use the
   `monitor` tool to delete it).
5. Delete this `UNINSTALL.md` (`packages/build-imessage/UNINSTALL.md`) — its
   absence signals the uninstall completed.
6. `restart` to bring the change live. The owner may also revoke Full Disk
   Access / Automation grants in System Settings if they want them gone.
