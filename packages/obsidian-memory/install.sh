#!/usr/bin/env bash
# Deterministic install for obsidian-memory: copy the skill verbatim and write
# the obsidian_memory config block. The interactive/customizable parts (vault
# mode, sync choice, layout, index scope, read-only vs writable paths) and the
# Python-dependency self-edit that pulls in the vector stack are NOT here — the
# agent does them from INSTALL.md.
#
# Parameters (env):
#   VAULT_PATH      absolute path to the Obsidian vault directory (required)
#   WRITABLE_PATHS  comma-separated, vault-relative dirs the agent may write to
#                   (optional; empty = read-only recall, capability follows this)
#   GATE_MODEL      model id for the relevance gate classifier, chosen at the
#                   install interview (optional; default openai/gpt-4.1-nano, a
#                   fast/cheap OpenRouter model). Only applied on first install,
#                   when the classifier def is seeded — never overwrites edits.
#
# The done-check gates the restart, but config.yaml is gitignored — a bad
# config write is NOT rolled back (the pre-restart config gate is the only
# protection). Paths are relative
# to the repo root.
set -euo pipefail

# Fail loud, first, if this Python cannot run the vector half of the index.
# Recall is one SQLite file holding both halves, and the vector half is a
# loadable extension — but `enable_load_extension` is compiled out of some
# Python builds, and there is deliberately no runtime fallback. Better to stop
# here naming the interpreter than to install a package whose every search
# raises. FTS5 is checked the same way; normally built in, but not always.
if ! uv run python - <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(":memory:")
try:
    conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
except sqlite3.OperationalError as exc:
    sys.exit(f"{sys.executable}: sqlite3 built without FTS5: {exc}")
if not hasattr(conn, "enable_load_extension"):
    sys.exit(
        f"{sys.executable}: sqlite3 built without extension loading "
        "(enable_load_extension) — sqlite-vec cannot load"
    )
try:
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.execute("select vec_version()").fetchone()
except Exception as exc:
    sys.exit(f"{sys.executable}: sqlite-vec failed to load: {exc}")
PY
then
  echo "obsidian-memory: aborting — the vector index cannot run on this" >&2
  echo "Python. Run 'uv sync' if the dependencies are not installed yet;" >&2
  echo "otherwise use a Python whose sqlite3 supports extension loading." >&2
  exit 1
fi

src="packages/obsidian-memory/skills/obsidian-memory"
dst="skills/obsidian-memory"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# The relevance gate is a core classifier, so its definition has to live in the
# classifiers dir, not the package. Never overwrite: the owner tunes this
# prompt in place, and a re-install must not silently revert their edits.
mkdir -p classifiers
if [ ! -f classifiers/memory-relevance.md ]; then
  cp packages/obsidian-memory/classifiers/memory-relevance.md \
    classifiers/memory-relevance.md
  # Pin the interview-chosen gate model into the freshly seeded def. The source
  # already carries the default; rewrite the one frontmatter line so a custom
  # choice sticks. sed -i.bak keeps this portable across GNU and BSD/macOS sed.
  gate_model="${GATE_MODEL:-openai/gpt-4.1-nano}"
  sed -i.bak -E "s|^model:.*|model: ${gate_model}|" \
    classifiers/memory-relevance.md
  rm -f classifiers/memory-relevance.md.bak
fi

: "${VAULT_PATH:?set VAULT_PATH to the Obsidian vault directory}"

# Build a YAML list of writable paths from the comma-separated input.
writable_yaml="["
if [ -n "${WRITABLE_PATHS:-}" ]; then
  IFS=',' read -ra parts <<<"$WRITABLE_PATHS"
  for path in "${parts[@]}"; do
    path="${path//[[:space:]]/}"
    [ -n "$path" ] && writable_yaml+="\"$path\","
  done
fi
writable_yaml+="]"

uv run python -m chief.config_apply \
  "obsidian_memory.vault_paths=[\"$VAULT_PATH\"]" \
  "obsidian_memory.writable_paths=$writable_yaml"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply obsidian-memory --source bundled
