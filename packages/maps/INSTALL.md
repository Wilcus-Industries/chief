# Installing maps

Works on any platform. Stdlib-only — no Python dependencies, no API keys, no
config.

## Steps

1. Run `bash packages/maps/install.sh` with the `shell` tool. It copies the
   skill and the bundled `maps_client.py` to `skills/maps/` and records the
   install in the registry (`chief.registry_apply`).
2. `restart` — the guarded commit brings the skill live.
3. Verify: `uv run python skills/maps/scripts/maps_client.py search
   "Statue of Liberty"` should return lat ≈ 40.689, lon ≈ -74.044.
4. Read the installed skill once — note the 1 req/s Nominatim limit the
   script enforces.
