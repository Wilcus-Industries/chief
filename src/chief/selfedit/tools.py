"""The native ``restart`` tool: the seatbelt over the agent's self-edits.

The agent edits its own files with ``write_file``/``edit_file`` (inert until
now), then calls ``restart`` to run the done-check and — on green — commit and
reboot into the change. There is no ``self_edit`` or ``install_package`` tool
anymore; editing is ordinary file work and installing is document-driven.
"""

from chief.provider.base import ToolSpec
from chief.selfedit.pipeline import SelfEditPipeline
from chief.tools import Tool, ToolContext, ToolRegistry

_RESTART_SPEC = ToolSpec(
    name="restart",
    description=(
        "Run the full done-check against your working tree, then restart into "
        "it. On green: your edits are committed and the daemon reboots into the "
        "new code (a boot failure auto-rolls-back). On red: your edits are kept "
        "in place and the check output comes back for you to fix forward. A "
        "restart with no repo changes is allowed (config reloads, script-only "
        "installs). The reboot happens at the safe turn boundary, after your "
        "reply is sent. `rationale` is the commit message for the change. "
        "When the tree is dirty, the first call returns the changed-file list "
        "for review — check every file belongs to this rationale, then call "
        "again with confirm=true."
    ),
    parameters={
        "type": "object",
        "properties": {
            "rationale": {"type": "string"},
            "confirm": {"type": "boolean"},
        },
        "required": ["rationale"],
    },
)


_REVERT_SPEC = ToolSpec(
    name="revert_edits",
    description=(
        "Discard your uncommitted changes to tracked repo files, restoring "
        "them to HEAD. The safe exit when the done-check keeps failing: "
        "instead of editing forward again, revert and rethink. Untracked "
        "files are left in place and reported."
    ),
    parameters={"type": "object", "properties": {}},
)


def register_restart_tool(registry: ToolRegistry, pipeline: SelfEditPipeline) -> None:
    """Expose the ``restart`` + ``revert_edits`` tools backed by the pipeline."""

    async def restart(
        rationale: str, confirm: bool = False, context: ToolContext | None = None
    ) -> str:
        # ``context`` is the calling thread, recorded so the rebooted daemon
        # reports "restart success" back where the restart was asked for.
        return await pipeline.restart(rationale, confirm, context)

    async def revert_edits() -> str:
        return await pipeline.revert_edits()

    registry.register(Tool(_RESTART_SPEC, restart, wants_context=True))
    registry.register(Tool(_REVERT_SPEC, revert_edits))
