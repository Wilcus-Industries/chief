#!/usr/bin/env bash
# Deterministic install for whisper: install whisper.cpp + ffmpeg, download
# the base model, and copy the skill verbatim. Nothing interactive.
#
# Paths are relative to the repo root. Requires Homebrew (macOS/Linuxbrew).
set -euo pipefail

# whisper-cli (whisper.cpp) — the local transcriber; ffmpeg — audio convert.
# Idempotent: skip whatever is already on PATH.
command -v whisper-cli >/dev/null 2>&1 || brew install whisper-cpp
command -v ffmpeg >/dev/null 2>&1 || brew install ffmpeg

# The base multilingual model (~148MB), pinned location the skill relies on.
model="data/whisper/ggml-base.bin"
if [ ! -f "$model" ]; then
  mkdir -p data/whisper
  curl -fL --retry 3 -o "$model" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
fi

src="packages/whisper/skills/whisper"
dst="skills/whisper"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply whisper --source bundled
