# Installing duckduckgo-search

Works on any platform. Gives chief keyless web search via the `ddgs` CLI.

## Steps

1. **Add the Python dependency (guarded self-edit).** With your file tools,
   add `ddgs` to `[project.dependencies]` in `pyproject.toml`, then run
   `uv sync` with the `shell` tool.
2. Run `bash packages/duckduckgo-search/install.sh` with the `shell` tool. It
   copies the skill verbatim to `skills/duckduckgo-search/` and records the
   install in the registry (`chief.registry_apply`). No config keys.
3. `restart` — the guarded commit brings the dependency and skill live.
4. Verify: `uv run ddgs text -q "test" -m 2 -o json` returns results. Empty
   output can mean momentary rate limiting — retry once after a pause before
   concluding it's broken.
5. Read the installed skill once — screening applies to everything fetched
   from the web.
