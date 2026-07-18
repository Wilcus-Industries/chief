"""The native ``restart`` tool: the seatbelt over the agent's self-edits.

The agent edits its own files with ``write_file``/``edit_file`` (inert until
now), then calls ``restart`` to run the done-check and — on green — commit and
reboot into the change. There is no ``self_edit`` or ``install_package`` tool
anymore; editing is ordinary file work and installing is document-driven.
"""

from chief.agent.tools import Tool, ToolRegistry
from chief.provider.base import ToolSpec
from chief.selfedit.pipeline import SelfEditPipeline

_RESTART_SPEC = ToolSpec(
    name="restart",
    description=(
        "Run the full done-check against your working tree, then restart into "
        "it. On green: your edits are committed and the daemon reboots into the "
        "new code (a boot failure auto-rolls-back). On red: your edits are kept "
        "in place and the check output comes back for you to fix forward. A "
        "restart with no repo changes is allowed (config reloads, script-only "
        "installs). The reboot happens at the safe turn boundary, after your "
        "reply is sent. `rationale` is the commit message for the change."
    ),
    parameters={
        "type": "object",
        "properties": {"rationale": {"type": "string"}},
        "required": ["rationale"],
    },
)


def register_restart_tool(registry: ToolRegistry, pipeline: SelfEditPipeline) -> None:
    """Expose the ``restart`` tool backed by the guarded pipeline."""

    async def restart(rationale: str) -> str:
        return await pipeline.restart(rationale)

    registry.register(Tool(_RESTART_SPEC, restart))
