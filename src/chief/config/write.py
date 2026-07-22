"""The config writer: deterministic deep-merge into config.yaml.

Writes go through here (via ``chief.config_apply`` and package install
scripts) so standard keys are set byte-exactly instead of retyped by the
model — and never appended, which the strict loader would reject as a
duplicate key on the next boot.
"""

from pathlib import Path
from typing import Any

import yaml

from chief.config.load import load_raw


def merge_config(updates: dict[str, Any], path: Path = Path("config.yaml")) -> None:
    """Deep-merge ``updates`` into config.yaml, creating it if absent.

    The deterministic config-writer package install scripts call (issue #185)
    so standard keys are set byte-exactly instead of retyped by the model.
    Nested mappings merge key-by-key; every other value is replaced.
    """
    raw = load_raw(path)
    path.write_text(yaml.safe_dump(_deep_merge(raw, updates), sort_keys=False))


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged
