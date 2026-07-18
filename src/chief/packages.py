"""Packages: manifest + INSTALL.md/UNINSTALL.md conventions the agent follows.

A package is a directory holding a ``manifest.yaml`` (name, description,
skills, config keys, secrets), an ``INSTALL.md`` the agent walks through to
install with the file tools, and an ``UNINSTALL.md`` whose final step deletes
itself as the completion signal. Discovery is done by the ``chief-pkg`` CLI
(see ``chief.pkgcli``); this module just parses manifests and scans the two
roots — bundled ``packages/`` and the local clone of the chief-packages repo.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CLONED_PACKAGES_DIR = Path("data/packages")


@dataclass(frozen=True)
class Package:
    """One package as declared by its manifest, plus where it was found."""

    name: str
    description: str
    path: Path
    skills: tuple[str, ...] = ()
    config_keys: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()

    def install_md(self) -> str:
        install = self.path / "INSTALL.md"
        return install.read_text() if install.exists() else ""


class PackageLibrary:
    """Scans the package roots; first root wins on a name collision."""

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


def validate(roots: tuple[Path, ...]) -> list[str]:
    """Return the well-formedness problems of every manifest under ``roots``.

    Empty means each manifest.yaml parses to a mapping with a non-empty name
    and description. Runs in the done-check so a broken manifest write fails
    and is rolled back rather than restarted into (issue #186).
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
        skills=tuple(meta.get("skills") or ()),
        config_keys=tuple(meta.get("config_keys") or ()),
        secrets=tuple(meta.get("secrets") or ()),
    )
