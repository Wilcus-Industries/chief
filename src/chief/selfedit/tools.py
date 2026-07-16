"""The native tools exposing the self-edit pipeline to the agent."""

from typing import Any

from chief.agent.tools import Tool, ToolRegistry
from chief.packages import PackageLibrary
from chief.provider.base import ToolSpec
from chief.selfedit.pipeline import SelfEditPipeline

_SPEC = ToolSpec(
    name="self_edit",
    description=(
        "Edit your own files (config, prompts, skills, source). Edits run "
        "through the guarded pipeline: they only stick if the full done-check "
        "passes, and the daemon restarts into the new code with automatic "
        "rollback on a failed boot. `files` maps repo-relative paths to their "
        "complete new contents."
    ),
    parameters={
        "type": "object",
        "properties": {
            "files": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "repo-relative path -> full new file content",
            },
            "rationale": {"type": "string"},
        },
        "required": ["files", "rationale"],
    },
)


def register_selfedit_tools(registry: ToolRegistry, pipeline: SelfEditPipeline) -> None:
    """Expose the self_edit tool backed by the pipeline."""

    async def self_edit(files: Any, rationale: str) -> str:
        if not isinstance(files, dict) or not files:
            return "error: files must be a non-empty object of path -> content"
        if not all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()
        ):
            return "error: files must map string paths to string contents"
        return await pipeline.apply(dict(files), rationale)

    registry.register(Tool(_SPEC, self_edit))


_INSTALL_SPEC = ToolSpec(
    name="install_package",
    description=(
        "Install a bundled or cloned package by name. Runs the package's (and "
        "its dependencies') install.sh under the guarded pipeline: copies "
        "skill files byte-for-byte, sets standard config keys, and wires MCP "
        "servers — deterministically, so it works with any model. Pass the "
        "parameters the package's INSTALL.md asks for as `env` (e.g. "
        "{\"IMESSAGE_HANDLES\": \"+15551234567\"}). One approval, one done-"
        "check, one restart for the whole dependency tree. Do the interactive "
        "and customizable steps (OS prompts, monitors) from INSTALL.md yourself."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "install parameters passed to every script",
            },
        },
        "required": ["name"],
    },
)


def register_install_tool(
    registry: ToolRegistry,
    pipeline: SelfEditPipeline,
    library: PackageLibrary,
) -> None:
    """Expose install_package, the one seatbelted package-installer runner."""

    async def install_package(name: str, env: Any = None) -> str:
        if env is None:
            env = {}
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            return "error: env must map string names to string values"
        try:
            order = library.install_order(name)
        except (KeyError, ValueError) as exc:
            return f"error: {exc}"
        scripts = [p.path / "install.sh" for p in order]
        missing = [p.name for p in order if not (p.path / "install.sh").exists()]
        if missing:
            return (
                "error: no install.sh for: "
                + ", ".join(missing)
                + " — follow each package's INSTALL.md manually"
            )
        try:
            return await pipeline.install(scripts, dict(env), f"install {name}")
        except RuntimeError as exc:
            return f"error: {exc}"

    registry.register(Tool(_INSTALL_SPEC, install_package))
