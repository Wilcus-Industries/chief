# Installing watchers

Works on any platform. Stdlib-only polling scripts — no dependencies, no
config keys. Watchers become useful when wired to the `schedule` tool; the
skill covers that.

## Steps

1. Run `bash packages/watchers/install.sh` with the `shell` tool. It copies
   the skill + scripts to `skills/watchers/`, creates `data/watcher-state/`,
   and records the install in the registry (`chief.registry_apply`).
2. `restart` — the guarded commit brings the skill live.
3. Verify: run the RSS watcher twice against any feed, e.g.
   `uv run python skills/watchers/scripts/watch_rss.py --name install-test
   --url https://hnrss.org/frontpage`. First run prints nothing (baseline);
   second run prints nothing or only genuinely-new items. Then clean up:
   `rm data/watcher-state/install-test.json`.
4. Read the installed skill once — the empty-stdout-means-silent contract is
   the whole point; a watcher that reports "nothing new" is a bug.
