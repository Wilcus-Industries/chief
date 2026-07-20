# Uninstalling whisper

1. Delete the installed skill dir `skills/whisper/` and the model dir
   `data/whisper/`.
2. Deregister: `uv run python -m chief.registry_apply whisper --remove`.
3. Delete this `UNINSTALL.md` (`packages/whisper/UNINSTALL.md`) — its
   absence signals the uninstall completed.
4. `restart` to bring the change live. The owner may also
   `brew uninstall whisper-cpp` (and `ffmpeg`, if nothing else uses it —
   check first; other packages rely on ffmpeg).
