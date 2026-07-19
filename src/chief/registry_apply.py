"""CLI: record a package install in ``data/installed.yaml`` deterministically.

Package ``install.sh`` scripts call this so registry membership is never left
to the model retyping YAML (the half-install failure mode: skills + config
land, the registry entry doesn't, and the hooks loader silently skips the
package). Idempotent — re-running an install updates the same entry::

    python -m chief.registry_apply obsidian-memory --source bundled

``--remove`` deregisters (the UNINSTALL.md counterpart). The read side lives
here too: :func:`load_installed` is the one registry loader boot, discovery,
and verify all share.
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

INSTALLED_REGISTRY = Path("data/installed.yaml")


def load_installed(path: Path = INSTALLED_REGISTRY) -> dict[str, object]:
    """The install registry: ``{name: {source}}`` (empty if absent).

    A registry that exists but doesn't parse to a mapping degrades to ``{}`` —
    which silently turns every package's hooks off at once — so that
    degradation must be LOUD: an ERROR naming the file, never a quiet empty."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        logger.error(
            "registry %s is not valid yaml — treating it as EMPTY, so every "
            "package's hooks are OFF until it is fixed (rewrite it with "
            "chief.registry_apply)", path, exc_info=True,
        )
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        logger.error(
            "registry %s parsed to %s, not a mapping — treating it as EMPTY, "
            "so every package's hooks are OFF until it is fixed (rewrite it "
            "with chief.registry_apply)", path, type(data).__name__,
        )
        return {}
    return data


def apply(
    name: str, source: str, path: Path = INSTALLED_REGISTRY, remove: bool = False
) -> None:
    """Set (or remove) ``name``'s registry entry, preserving the others.

    Reads through :func:`load_installed` so a corrupt registry degrades the
    same LOUD way everywhere — and the documented remedy (rewrite it with
    chief.registry_apply) is exactly this function running."""
    data = load_installed(path)
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
