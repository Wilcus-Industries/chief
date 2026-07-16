"""Native tools for the agent to manage its own monitors."""

from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.monitors.service import MonitorService
from chief.provider.base import ToolSpec

_CREATE_SPEC = ToolSpec(
    name="create_monitor",
    description=(
        "Watch a channel's inbound events and wake this thread when one "
        "matches. Give exactly one of `pattern` (regex over the event text, "
        "cheap) or `instruction` (a small-model yes/no judgment)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "pattern": {"type": "string"},
            "instruction": {"type": "string"},
            "watch_channel": {
                "type": "string",
                "description": "channel to watch; defaults to this thread's channel",
            },
        },
        "required": ["description"],
    },
)

_LIST_SPEC = ToolSpec(
    name="list_monitors",
    description="List all active monitors.",
    parameters={"type": "object", "properties": {}},
)

_DELETE_SPEC = ToolSpec(
    name="delete_monitor",
    description="Delete a monitor by id.",
    parameters={
        "type": "object",
        "properties": {"monitor_id": {"type": "integer"}},
        "required": ["monitor_id"],
    },
)


def register_monitor_tools(registry: ToolRegistry, service: MonitorService) -> None:
    """Expose create/list/delete monitor tools backed by the service."""

    async def create_monitor(
        description: str,
        context: ToolContext | None = None,
        pattern: str | None = None,
        instruction: str | None = None,
        watch_channel: str | None = None,
    ) -> str:
        if context is None:
            return "error: create_monitor needs a session context"
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

    async def list_monitors() -> str:
        rows = await service.list_enabled()
        if not rows:
            return "no monitors"
        return "\n".join(
            f"#{r.id} [{r.watch_channel}] {r.description} -> wakes {r.wake_thread}"
            for r in rows
        )

    async def delete_monitor(monitor_id: int) -> str:
        deleted = await service.delete(monitor_id)
        return f"monitor #{monitor_id} deleted" if deleted else "error: no such monitor"

    registry.register(Tool(_CREATE_SPEC, create_monitor, wants_context=True))
    registry.register(Tool(_LIST_SPEC, list_monitors))
    registry.register(Tool(_DELETE_SPEC, delete_monitor))
