"""``chief-pkg``: the package-discovery CLI the agent runs via Bash.

Discovery only — ``search <query>`` and ``list``, each with ``--installed``.
Output names each package with its description, source (bundled vs cloned),
installed status, and on-disk path, so the agent reads and edits it in place.
The CLI owns a local clone of the chief-packages repo and reads across two
roots — bundled ``packages/`` and the clone — bundled winning name collisions.
Install/uninstall are document-driven; there is no ``remove`` here (PRD #198).

The clone is refreshed on **every** invocation, and ``update`` does the same
thing explicitly and reports what moved. The split from ``chief update``
matters: that one moves core (plus the bundled packages, same repo) and
restarts the daemon; this one only moves the clone, which is data the agent
reads — no running code changes, so nothing restarts. Both the auto-pull and
the one-time clone are bounded and fail soft: a stale clone beats a wedged CLI,
and ``chief-pkg`` runs through the single dispatcher, so hanging here hangs the
whole daemon.
"""

import argparse
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from chief.config import Config, load_config, load_raw
from chief.packages import CLONED_PACKAGES_DIR, Package, PackageLibrary, dep_importable
from chief.pkgsync import clone_if_missing, pull_clone
from chief.registry_apply import load_installed as _load_installed

INSTALLED_REGISTRY = Path("data/installed.yaml")


@dataclass(frozen=True)
class Row:
    """One discovered package as the CLI reports it."""

    name: str
    description: str
    source: str
    installed: bool
    path: Path


def discover(
    bundled_root: Path, clone_root: Path, installed: Mapping[str, object]
) -> list[Row]:
    """Scan both roots (bundled wins collisions) into installed-tagged rows."""
    library = PackageLibrary((bundled_root, clone_root))
    rows = []
    for package in library.scan():
        source = "bundled" if _under(package, bundled_root) else "cloned"
        rows.append(Row(
            name=package.name,
            description=package.description,
            source=source,
            installed=package.name in installed,
            path=package.path,
        ))
    return sorted(rows, key=lambda r: r.name)


def _under(package: Package, root: Path) -> bool:
    try:
        package.path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _config_has(config_raw: Mapping[str, object], key: str) -> bool:
    """Is a manifest's dotted ``config_keys`` entry present in the raw config?

    Manifests declare keys the way ``config_apply`` takes them
    (``imessage.enabled``) while the raw config is nested, so a flat
    membership test called every nested key absent — no package declaring one
    could ever verify as installed. A non-mapping partway down is a
    misconfiguration, reported like an absent key rather than raised.
    """
    node: object = config_raw
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False
        node = node[part]
    return True


def verify_install(
    package: Package,
    installed: Mapping[str, object],
    config_raw: Mapping[str, object],
    skills_root: Path = Path("skills"),
    secrets_root: Path = Path("secrets"),
) -> list[str]:
    """Postcondition check for a package install; empty means fully installed.

    Everything the manifest declares must actually have landed — half-installs
    (skills copied but no registry entry, config keys missing) were silent
    before: the hooks loader just skipped the package and discovery reported
    it uninstalled.
    """
    problems = []
    if package.name not in installed:
        problems.append(
            f"not registered in data/installed.yaml — run: uv run python -m "
            f"chief.registry_apply {package.name}"
        )
    for skill in package.skills:
        # Manifests list package-relative paths (skills/<name>); the install
        # lands at skills_root/<name> — compare on the basename.
        name = Path(skill).name
        if not (skills_root / name / "SKILL.md").exists():
            problems.append(f"skill '{name}' missing at {skills_root / name}")
    for key in package.config_keys:
        if not _config_has(config_raw, key):
            problems.append(f"config key '{key}' absent from config.yaml")
    for secret in package.secrets:
        if not (secrets_root / secret).exists():
            problems.append(f"secret file '{secret}' absent from {secrets_root}/")
    for dep in package.python_deps:
        if not dep_importable(dep):
            problems.append(
                f"python dependency '{dep}' not importable (python_deps "
                "lists import names) — add its distribution and `uv sync`"
            )
    return problems


def _matches(row: Row, query: str) -> bool:
    needle = query.lower()
    return needle in row.name.lower() or needle in row.description.lower()


def render(rows: list[Row]) -> str:
    if not rows:
        return "no packages found"
    lines = []
    for row in rows:
        status = "installed" if row.installed else "available"
        lines.append(f"{row.name}  [{status}] ({row.source})  {row.path}")
        lines.append(f"    {row.description}")
    return "\n".join(lines)


def _run(argv: list[str], rows: list[Row]) -> str:
    parser = argparse.ArgumentParser(prog="chief-pkg")
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search", help="find packages by name/description")
    search.add_argument("query")
    search.add_argument("--installed", action="store_true")
    listing = sub.add_parser("list", help="list all packages")
    listing.add_argument("--installed", action="store_true")
    # Handled in main() before we get here; declared so it shows up in --help.
    sub.add_parser("update", help="pull the packages clone and report")
    sub.add_parser("verify", help="check a package is fully installed")
    args = parser.parse_args(argv)
    if args.installed:
        rows = [r for r in rows if r.installed]
    if args.command == "search":
        rows = [r for r in rows if _matches(r, args.query)]
    return render(rows)


def _run_verify(name: str, config: Config) -> None:
    library = PackageLibrary((config.packages_dir, CLONED_PACKAGES_DIR))
    package = library.get(name)
    if package is None:
        raise SystemExit(f"no such package: {name}")
    problems = verify_install(
        package, _load_installed(INSTALLED_REGISTRY), load_raw()
    )
    if problems:
        print(f"{name}: install INCOMPLETE")
        for problem in problems:
            print(f"  - {problem}")
        raise SystemExit(1)
    print(f"verified: {name} is fully installed")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    config = load_config()
    clone_if_missing(config.packages_repo, CLONED_PACKAGES_DIR)
    # Every invocation refreshes the clone: the agent reads packages straight
    # off disk, so a stale clone silently serves yesterday's skills.
    summary = pull_clone(CLONED_PACKAGES_DIR)
    if args[:1] == ["update"]:
        print(f"packages: {summary}")
        return
    if args[:1] == ["verify"]:
        if len(args) != 2:
            raise SystemExit("usage: chief-pkg verify <name>")
        _run_verify(args[1], config)
        return
    rows = discover(
        config.packages_dir, CLONED_PACKAGES_DIR, _load_installed(INSTALLED_REGISTRY)
    )
    print(_run(args, rows))
