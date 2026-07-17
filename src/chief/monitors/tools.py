"""Native tool for the agent to manage its own monitors."""

from typing import Any

from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.monitors.service import MonitorService
from chief.provider.base import ToolSpec

_SPEC = ToolSpec(
    name="monitor",
    description=(
        "Manage monitors that watch a channel and wake this thread on a match. "
        "action=create needs `description` and exactly one of `pattern` (regex "
        "over event text, cheap) or `instruction` (a small-model yes/no), plus "
        "optional `watch_channel` (defaults to this thread's channel). "
        "action=list takes nothing. action=delete needs `monitor_id`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "list", "delete"]},
            "description": {"type": "string"},
            "pattern": {"type": "string"},
            "instruction": {"type": "string"},
            "watch_channel": {
                "type": "string",
                "description": "channel to watch; defaults to this thread's channel",
            },
            "monitor_id": {"type": "integer"},
        },
        "required": ["action"],
    },
)


def register_monitor_tools(registry: ToolRegistry, service: MonitorService) -> None:
    """Expose the monitor tool (create/list/delete) backed by the service."""

    async def _create(
        context: ToolContext | None,
        description: str | None,
        pattern: str | None,
        instruction: str | None,
        watch_channel: str | None,
    ) -> str:
        if context is None:
            return "error: create needs a session context"
        if not description:
            return "error: create needs a description"
        if (pattern is None) == (instruction is None):
            return "error: give exactly one of pattern or instruction"
        predicate = (
            {"kind": "code", "field": "text", "pattern": pattern}
            if pattern is not None
            else {"kind": "model", "instruction": instruction}
        )
        monitor_id = await service.create(
            description=description,
            watch_channel=watch_channel or context.channel,
            wake_channel=context.channel,
            wake_thread=context.thread_key,
            predicate=predicate,
        )
        return f"monitor #{monitor_id} created"

    async def _list() -> str:
        rows = await service.list_enabled()
        if not rows:
            return "no monitors"
        return "\n".join(
            f"#{r.id} [{r.watch_channel}] {r.description} -> wakes {r.wake_thread}"
            for r in rows
        )

    async def monitor(
        action: str,
        context: ToolContext | None = None,
        description: str | None = None,
        pattern: str | None = None,
        instruction: str | None = None,
        watch_channel: str | None = None,
        monitor_id: Any = None,
    ) -> str:
        if action == "create":
            return await _create(
                context, description, pattern, instruction, watch_channel
            )
        if action == "list":
            return await _list()
        if action == "delete":
            if not isinstance(monitor_id, int):
                return "error: delete needs monitor_id"
            deleted = await service.delete(monitor_id)
            return (
                f"monitor #{monitor_id} deleted"
                if deleted
                else "error: no such monitor"
            )
        return f"error: unknown action '{action}'"

    registry.register(Tool(_SPEC, monitor, wants_context=True))
