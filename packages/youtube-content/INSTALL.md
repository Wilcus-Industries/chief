# Installing youtube-content

Works on any platform. One small Python dependency.

## Steps

1. **Add the Python dependency (guarded self-edit).** With your file tools,
   add `youtube-transcript-api` to `[project.dependencies]` in
   `pyproject.toml`, then run `uv sync` with the `shell` tool. (Import name
   is `youtube_transcript_api` — the manifest lists that.)
2. Run `bash packages/youtube-content/install.sh` with the `shell` tool. It
   copies the skill + fetch script to `skills/youtube-content/` and records
   the install in the registry (`chief.registry_apply`). No config keys.
3. `restart` — the guarded commit brings the dependency and skill live.
4. Verify: fetch any public video with captions, e.g.
   `uv run python skills/youtube-content/scripts/fetch_transcript.py
   "https://www.youtube.com/watch?v=jNQXAC9IVRw" --text-only` — non-empty
   text means it works.
5. Read the installed skill once — transcribe-before-talking is the rule
   that matters.
