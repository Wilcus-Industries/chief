"""CLI: apply dotted ``key=value`` settings to config.yaml deterministically.

Package ``install.sh`` scripts call this to set standard config keys without
the model retyping YAML (issue #185). Each argument is ``dotted.key=<value>``
where the value parses as YAML, so bools, numbers, and lists work::

    python -m chief.config_apply imessage.enabled=true \
        'imessage.owner_handles=["+15551234567"]'

Runs inside the self-edit seatbelt, so a bad write is rolled back.
"""

import sys
from typing import Any

import yaml

from chief.config import merge_config


def _nest(dotted: str, value: Any) -> dict[str, Any]:
    parts = dotted.split(".")
    node: dict[str, Any] = {parts[-1]: value}
    for key in reversed(parts[:-1]):
        node = {key: node}
    return node


def main(argv: list[str]) -> None:
    for arg in argv:
        if "=" not in arg:
            raise SystemExit(f"bad setting (need dotted.key=value): {arg}")
        dotted, raw = arg.split("=", 1)
        merge_config(_nest(dotted, yaml.safe_load(raw)))


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
