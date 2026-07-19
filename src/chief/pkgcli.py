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
import importlib.util
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from chief.config import Config, load_config, load_raw
from chief.packages import CLONED_PACKAGES_DIR, Package, PackageLibrary
from chief.registry_apply import load_installed as _load_installed

INSTALLED_REGISTRY = Path("data/installed.yaml")

#: Hard ceiling on the one-time clone so a stalled network or auth prompt can never
#: wedge `chief-pkg` (and, through the single dispatcher, the whole daemon).
CLONE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class Row:
    """One discovered package as the CLI reports it."""

    name: str
    description: str
    source: str
    installed: bool
    path: Path


def clone_if_missing(repo_url: str, dest: Path) -> None:
    """Clone the chief-packages repo once; never fail OR hang the CLI if it can't.

    The remote may not exist yet, so a clone failure is a warning, not an error —
    discovery still works over the bundled root alone. ``GIT_TERMINAL_PROMPT=0`` stops
    git blocking forever on an interactive auth prompt, and a hard ``timeout`` bounds a
    stalled network; either way discovery falls back to bundled-only.
    """
    if dest.exists() or not repo_url:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dest)],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"warning: clone of {repo_url} timed out after "
              f"{CLONE_TIMEOUT_SECONDS:g}s", file=sys.stderr)
        return
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
        if not (skills_root / skill / "SKILL.md").exists():
            problems.append(f"skill '{skill}' missing at {skills_root / skill}")
    for key in package.config_keys:
        if key not in config_raw:
            problems.append(f"config key '{key}' absent from config.yaml")
    for secret in package.secrets:
        if not (secrets_root / secret).exists():
            problems.append(f"secret file '{secret}' absent from {secrets_root}/")
    for dep in package.python_deps:
        if importlib.util.find_spec(dep) is None:
            problems.append(
                f"python dependency '{dep}' not importable — add it to "
                f"pyproject.toml and run `uv sync`"
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
    if args[:1] == ["verify"]:
        if len(args) != 2:
            raise SystemExit("usage: chief-pkg verify <name>")
        _run_verify(args[1], config)
        return
    rows = discover(
        config.packages_dir, CLONED_PACKAGES_DIR, _load_installed(INSTALLED_REGISTRY)
    )
    print(_run(args, rows))
