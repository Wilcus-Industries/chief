# Installing whisper

Needs Homebrew. Downloads ~148MB (model) plus the whisper-cpp and ffmpeg
formulae — mention that to the owner before running.

## Steps

1. Run `bash packages/whisper/install.sh` with the `shell` tool (raise the
   shell timeout — the model download takes a while). It installs
   `whisper-cpp` + `ffmpeg`, fetches `ggml-base.bin` to `data/whisper/`,
   copies the skill verbatim to `skills/whisper/`, and records the install
   in the registry (`chief.registry_apply`). No config keys.
2. `restart` — the guarded commit brings the skill live.
3. Verify end-to-end: generate a spoken test clip and transcribe it —
   `say -o /tmp/t.aiff "testing one two three"` (macOS), then
   `ffmpeg -y -i /tmp/t.aiff -ar 16000 -ac 1 /tmp/t.wav`, then
   `whisper-cli -m data/whisper/ggml-base.bin -f /tmp/t.wav -np -nt` —
   confirm the words come back.
4. Read the installed skill once — note the privacy rule for owner-voice
   transcripts and the hallucination caveat.
