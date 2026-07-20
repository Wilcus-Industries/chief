"""Packages: manifest + INSTALL.md/UNINSTALL.md conventions the agent follows.

A package is a directory holding a ``manifest.yaml`` (name, description,
skills, config keys, secrets), an ``INSTALL.md`` the agent walks through to
install with the file tools, and an ``UNINSTALL.md`` whose final step deletes
itself as the completion signal. Discovery is done by the ``chief-pkg`` CLI
(see ``chief.pkgcli``); this module just parses manifests and scans the two
roots — bundled ``packages/`` and the local clone of the chief-packages repo.
"""

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CLONED_PACKAGES_DIR = Path("data/packages")


def dep_importable(dep: str) -> bool:
    """Whether one manifest ``python_deps`` entry imports.

    ``python_deps`` lists IMPORT names (``yaml``), never distribution names
    (``PyYAML``). A dotted name whose parent package is absent makes
    ``find_spec`` raise instead of returning None — same answer: not there."""
    try:
        return importlib.util.find_spec(dep) is not None
    except ModuleNotFoundError:
        return False


@dataclass(frozen=True)
class HookSpec:
    """Where a package's agent-loop hooks live: a module file and its
    ``register(context, hooks)`` entry point, both relative to the package dir."""

    module: str
    register: str


@dataclass(frozen=True)
class McpServerSpec:
    """One MCP server a package declares — same shape as a ``config.yaml``
    ``mcp_servers`` entry (``config.Config.mcp_servers``, ``wiring.build_mcp``)."""

    name: str
    url: str | None = None
    command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Package:
    """One package as declared by its manifest, plus where it was found."""

    name: str
    description: str
    path: Path
    skills: tuple[str, ...] = ()
    config_keys: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    python_deps: tuple[str, ...] = ()
    hooks: HookSpec | None = None
    mcp_servers: tuple[McpServerSpec, ...] = ()

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
    problems.extend(_validate_hooks(manifest, meta.get("hooks")))
    problems.extend(_validate_mcp_servers(manifest, meta.get("mcp_servers")))
    return problems


def _validate_hooks(manifest: Path, hooks: object) -> list[str]:
    """A declared ``hooks`` block must be a mapping with non-empty ``module``
    and ``register``; a malformed one fails the done-check and is rolled back."""
    if hooks is None:
        return []
    if (
        not isinstance(hooks, dict)
        or not str(hooks.get("module") or "").strip()
        or not str(hooks.get("register") or "").strip()
    ):
        return [f"{manifest}: 'hooks' must set non-empty 'module' and 'register'"]
    return []


def _validate_mcp_servers(manifest: Path, mcp_servers: object) -> list[str]:
    """A declared ``mcp_servers`` block must map to mappings, each setting
    exactly one of ``url`` or ``command`` (``ServerConfig``'s own rule) —
    malformed fails the done-check and is rolled back, same as ``hooks``."""
    if mcp_servers is None:
        return []
    if not isinstance(mcp_servers, dict):
        return [f"{manifest}: 'mcp_servers' must be a mapping"]
    problems = []
    for name, entry in mcp_servers.items():
        if not isinstance(entry, dict):
            problems.append(f"{manifest}: mcp_servers.{name} must be a mapping")
        elif bool(str(entry.get("url") or "").strip()) == bool(entry.get("command")):
            problems.append(  # neither set, or both set
                f"{manifest}: mcp_servers.{name} must set exactly one of "
                "'url' or 'command'"
            )
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
        python_deps=tuple(meta.get("python_deps") or ()),
        hooks=_parse_hooks(meta.get("hooks")),
        mcp_servers=_parse_mcp_servers(meta.get("mcp_servers")),
    )


def _parse_hooks(hooks: object) -> HookSpec | None:
    """Build a HookSpec from a well-formed mapping; drop anything else silently
    (a malformed block fails loudly in validate() instead — see _validate_hooks)."""
    if isinstance(hooks, dict) and hooks.get("module") and hooks.get("register"):
        return HookSpec(module=str(hooks["module"]), register=str(hooks["register"]))
    return None


def _parse_mcp_servers(mcp_servers: object) -> tuple[McpServerSpec, ...]:
    """Build one spec per well-formed entry; drop a malformed block/entry and
    any unrecognized key inside one silently (validate() is the loud path)."""
    if not isinstance(mcp_servers, dict):
        return ()
    return tuple(
        McpServerSpec(
            name=str(name),
            url=entry.get("url"),
            command=tuple(entry["command"]) if entry.get("command") else None,
        )
        for name, entry in mcp_servers.items()
        if isinstance(entry, dict)
    )
