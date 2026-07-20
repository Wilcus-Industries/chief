# Uninstalling claude-code

1. Delete the installed skill dir `skills/claude-code/`.
2. Deregister: `uv run python -m chief.registry_apply claude-code --remove`.
3. Delete this `UNINSTALL.md` (`packages/claude-code/UNINSTALL.md`) — its
   absence signals the uninstall completed.
4. `restart` to bring the change live. The CLI itself is the owner's
   (`npm uninstall -g @anthropic-ai/claude-code` if they want it gone).
