"""Packages: manifest + INSTALL.md conventions the agent follows.

A package is a directory holding a ``manifest.yaml`` (name, description,
MCP servers, skills to link, config keys, secrets, dependencies) and an
``INSTALL.md`` the agent walks through. Install is agent-driven — the
core install-package skill guides it; there is no package manager. Core
scans bundled packages plus a local clone of the public chief-packages
repo.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CLONED_PACKAGES_DIR = Path("data/packages")


@dataclass(frozen=True)
class Package:
    """One package as declared by its manifest."""

    name: str
    description: str
    path: Path
    dependencies: tuple[str, ...] = ()
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    skills: tuple[str, ...] = ()
    config_keys: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()

    def install_md(self) -> str:
        install = self.path / "INSTALL.md"
        return install.read_text() if install.exists() else ""


class PackageLibrary:
    """Scans package roots and resolves dependency-ordered installs."""

    def __init__(self, roots: tuple[Path, ...]) -> None:
        self._roots = roots

    def scan(self) -> list[Package]:
        packages: dict[str, Package] = {}
        for root in self._roots:
            for manifest in sorted(root.glob("*/manifest.yaml")):
                package = _parse(manifest)
                # First root wins on a name collision (bundled beats cloned).
                if package is not None and package.name not in packages:
                    packages[package.name] = package
        return list(packages.values())

    def get(self, name: str) -> Package | None:
        return next((p for p in self.scan() if p.name == name), None)

    def install_order(self, name: str) -> list[Package]:
        """The package plus its dependency tree, dependencies first."""
        packages = {p.name: p for p in self.scan()}
        order: list[Package] = []
        seen: set[str] = set()

        def visit(pkg_name: str, trail: tuple[str, ...]) -> None:
            if pkg_name in trail:
                raise ValueError(f"dependency cycle: {' -> '.join(trail)}")
            if pkg_name in seen:
                return
            package = packages.get(pkg_name)
            if package is None:
                raise KeyError(f"unknown package '{pkg_name}'")
            for dep in package.dependencies:
                visit(dep, (*trail, pkg_name))
            seen.add(pkg_name)
            order.append(package)

        visit(name, ())
        return order


def validate(roots: tuple[Path, ...]) -> list[str]:
    """Return the well-formedness problems of every manifest under roots.

    Empty means each manifest.yaml parses to a mapping with a non-empty name
    and description. Runs in the self-edit done-check alongside skill
    validation so a broken manifest is rolled back, not merged (issue #186).
    """
    problems: list[str] = []
    for root in roots:
        for manifest in sorted(root.glob("*/manifest.yaml")):
            problems.extend(_validate_manifest(manifest))
    return problems


def _validate_manifest(manifest: Path) -> list[str]:
    try:
        meta = yaml.safe_load(manifest.read_text())
    except yaml.YAMLError as exc:
        return [f"{manifest}: not valid yaml ({exc})"]
    if not isinstance(meta, dict):
        return [f"{manifest}: not a mapping"]
    problems = []
    if not str(meta.get("name") or "").strip():
        problems.append(f"{manifest}: missing 'name'")
    if not str(meta.get("description") or "").strip():
        problems.append(f"{manifest}: missing 'description'")
    return problems


def _parse(manifest: Path) -> Package | None:
    try:
        meta = yaml.safe_load(manifest.read_text()) or {}
    except yaml.YAMLError:
        logger.warning("package manifest %s is not valid yaml; skipping", manifest)
        return None
    return Package(
        name=str(meta.get("name") or manifest.parent.name),
        description=str(meta.get("description") or "").strip(),
        path=manifest.parent,
        dependencies=tuple(meta.get("dependencies") or ()),
        mcp_servers=dict(meta.get("mcp_servers") or {}),
        skills=tuple(meta.get("skills") or ()),
        config_keys=tuple(meta.get("config_keys") or ()),
        secrets=tuple(meta.get("secrets") or ()),
    )
