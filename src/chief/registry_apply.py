"""CLI: record a package install in ``data/installed.yaml`` deterministically.

Package ``install.sh`` scripts call this so registry membership is never left
to the model retyping YAML (the half-install failure mode: skills + config
land, the registry entry doesn't, and the hooks loader silently skips the
package). Idempotent — re-running an install updates the same entry::

    python -m chief.registry_apply obsidian-memory --source bundled

``--remove`` deregisters (the UNINSTALL.md counterpart).
"""

import argparse
import sys
from pathlib import Path

import yaml

INSTALLED_REGISTRY = Path("data/installed.yaml")


def apply(
    name: str, source: str, path: Path = INSTALLED_REGISTRY, remove: bool = False
) -> None:
    """Set (or remove) ``name``'s registry entry, preserving the others."""
    data: dict[str, object] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text())
        if isinstance(loaded, dict):
            data = loaded
    if remove:
        data.pop(name, None)
    else:
        data[name] = {"source": source}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="chief.registry_apply")
    parser.add_argument("name")
    parser.add_argument("--source", choices=("bundled", "cloned"), default="bundled")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    apply(args.name, args.source, remove=args.remove)
    verb = "deregistered" if args.remove else "registered"
    print(f"{verb} {args.name} in {INSTALLED_REGISTRY}")


if __name__ == "__main__":  # pragma: no cover
    main()
