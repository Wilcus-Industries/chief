"""``chief-pkg``: the package-discovery CLI the agent runs via Bash.

Discovery only — ``search <query>`` and ``list``, each with ``--installed``.
Output names each package with its description, source (bundled vs cloned),
installed status, and on-disk path, so the agent reads and edits it in place.
The CLI owns a local clone of the chief-packages repo (clone-if-missing, no
auto-pull) and reads across two roots — bundled ``packages/`` and the clone —
bundled winning name collisions. Install/uninstall are document-driven; there
is no ``pull`` or ``remove`` here (PRD #198).
"""

import argparse
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from chief.config import load_config
from chief.packages import CLONED_PACKAGES_DIR, Package, PackageLibrary

INSTALLED_REGISTRY = Path("data/installed.yaml")


@dataclass(frozen=True)
class Row:
    """One discovered package as the CLI reports it."""

    name: str
    description: str
    source: str
    installed: bool
    path: Path


def _load_installed(path: Path) -> dict[str, object]:
    """The agent-written registry: ``{name: {source, commit}}`` (empty if absent)."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else {}


def clone_if_missing(repo_url: str, dest: Path) -> None:
    """Clone the chief-packages repo once; never fail the CLI if it can't.

    The remote may not exist yet, so a clone failure is a warning, not an
    error — discovery still works over the bundled root alone.
    """
    if dest.exists() or not repo_url:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"warning: could not clone {repo_url}: {result.stderr.strip()}",
              file=sys.stderr)


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
    args = parser.parse_args(argv)
    if args.installed:
        rows = [r for r in rows if r.installed]
    if args.command == "search":
        rows = [r for r in rows if _matches(r, args.query)]
    return render(rows)


def main(argv: list[str] | None = None) -> None:
    config = load_config()
    clone_if_missing(config.packages_repo, CLONED_PACKAGES_DIR)
    rows = discover(
        config.packages_dir, CLONED_PACKAGES_DIR, _load_installed(INSTALLED_REGISTRY)
    )
    print(_run(sys.argv[1:] if argv is None else argv, rows))
