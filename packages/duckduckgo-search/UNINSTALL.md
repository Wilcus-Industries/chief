# Uninstalling duckduckgo-search

1. Remove `ddgs` from `[project.dependencies]` in `pyproject.toml` (guarded
   self-edit) and run `uv sync`.
2. Delete the installed skill dir `skills/duckduckgo-search/`.
3. Deregister: `uv run python -m chief.registry_apply duckduckgo-search --remove`.
4. Delete this `UNINSTALL.md` (`packages/duckduckgo-search/UNINSTALL.md`) —
   its absence signals the uninstall completed.
5. `restart` to bring the change live.
