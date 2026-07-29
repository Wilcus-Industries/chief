"""The config writer: deterministic deep-merge into config.yaml.

Writes go through here (via ``chief.config_apply`` and package install
scripts) so standard keys are set byte-exactly instead of retyped by the
model — and never appended, which the strict loader would reject as a
duplicate key on the next boot.

:func:`set_dedicated_mode` is the surgical counterpart, for the one write the
installer makes into a file that is still all comments.
"""

import re
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


def set_dedicated_mode(config_path: Path = Path("config.yaml")) -> bool:
    """Flip ``imessage.mode`` to ``dedicated`` in place (#286).

    A surgical edit, not a re-dump: at install time config.yaml is still the
    seeded template and a yaml round-trip would strip every comment in it.
    Anchored on the ``imessage:`` block — config.yaml is user-ordered and
    package-extended, so the first ``mode:`` in the file is not necessarily
    this one.
    """
    if not config_path.exists():
        return False
    text = config_path.read_text()
    block = re.search(r"^imessage:[ \t]*$", text, re.M)
    if block is None:
        return False
    match = re.search(r"^([ \t]+)mode:[ \t]*\S+[ \t]*$", text[block.end():], re.M)
    if match is None:
        return False
    start, end = block.end() + match.start(), block.end() + match.end()
    config_path.write_text(
        text[:start] + f"{match.group(1)}mode: dedicated" + text[end:]
    )
    return True


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged
