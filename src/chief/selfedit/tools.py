"""The native tool exposing the self-edit pipeline to the agent."""

from typing import Any

from chief.agent.tools import Tool, ToolRegistry
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
