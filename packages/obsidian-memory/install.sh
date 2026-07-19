#!/bin/bash
set -e

# Default values
WRITABLE_PATHS="${WRITABLE_PATHS:-}"

# Validate inputs
if [[ -z "$VAULT_PATH" ]]; then
  echo "Usage: VAULT_PATH=<path> [WRITABLE_PATHS=<comma-sep dirs or empty>] $0"
  exit 1
fi

# Create skill directory
mkdir -p "skills/obsidian-memory"

# Write skill file (stub)
cat > "skills/obsidian-memory/skill.md" <<EOF
# obsidian-memory skill
# This is a placeholder. The real logic is in the provider configuration.
EOF

# Update config.yaml
cat >> "config.yaml" <<EOF

obsidian_memory:
  vault_paths:
    - $VAULT_PATH
  writable_paths:
    - $WRITABLE_PATHS
EOF

# Record installation
mkdir -p data
if [[ ! -f "data/installed.yaml" ]]; then
  echo "{}" > "data/installed.yaml"
fi
echo "obsidian-memory:" >> "data/installed.yaml"
echo "  source: bundled" >> "data/installed.yaml"

echo "Installed obsidian-memory for vault $VAULT_PATH."