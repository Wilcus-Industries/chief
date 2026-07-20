# Uninstalling youtube-content

1. Remove `youtube-transcript-api` from `[project.dependencies]` in
   `pyproject.toml` (guarded self-edit) and run `uv sync`.
2. Delete the installed skill dir `skills/youtube-content/`.
3. Deregister: `uv run python -m chief.registry_apply youtube-content --remove`.
4. Delete this `UNINSTALL.md` (`packages/youtube-content/UNINSTALL.md`) —
   its absence signals the uninstall completed.
5. `restart` to bring the change live.
