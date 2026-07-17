"""Read-only agent tools over a PackageLibrary: list and inspect packages."""

import yaml

from chief.agent.tools import Tool, ToolRegistry
from chief.packages import CLONED_PACKAGES_DIR, PackageLibrary
from chief.provider.base import ToolSpec

_LIST_SPEC = ToolSpec(
    name="list_packages",
    description="List packages, each tagged [installed] or [available].",
    parameters={"type": "object", "properties": {}},
    read_only=True,
)

_INFO_SPEC = ToolSpec(
    name="package_info",
    description=(
        "A package's manifest, dependency-ordered install list, and its "
        "INSTALL.md instructions. Use with the install-package skill."
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
    read_only=True,
)


def register_package_tools(
    registry: ToolRegistry, library: PackageLibrary, repo_url: str
) -> None:
    """Expose the read-only package tools backed by the library."""

    async def list_packages() -> str:
        packages = library.scan()
        lines = [
            f"- [{'installed' if library.is_installed(p) else 'available'}] "
            f"{p.name}: {p.description}"
            for p in packages
        ]
        if not lines:
            lines = ["(no packages found)"]
        lines.append(
            f"More packages: clone {repo_url} into {CLONED_PACKAGES_DIR} "
            "and list again."
        )
        return "\n".join(lines)

    async def package_info(name: str) -> str:
        try:
            order = library.install_order(name)
        except (KeyError, ValueError) as exc:
            return f"error: {exc}"
        package = order[-1]
        deps = " -> ".join(p.name for p in order)
        manifest = yaml.safe_dump(
            {
                "name": package.name,
                "description": package.description,
                "dependencies": list(package.dependencies),
                "mcp_servers": package.mcp_servers,
                "skills": list(package.skills),
                "config_keys": list(package.config_keys),
                "secrets": list(package.secrets),
            },
            sort_keys=False,
        )
        return (
            f"install order (dependencies first): {deps}\n\n"
            f"manifest:\n{manifest}\nINSTALL.md:\n{package.install_md()}"
        )

    registry.register(Tool(_LIST_SPEC, list_packages))
    registry.register(Tool(_INFO_SPEC, package_info))
