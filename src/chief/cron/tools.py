"""Native tools for the agent to manage its own schedules."""

from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.cron.service import CronService
from chief.provider.base import ToolSpec

_CREATE_SPEC = ToolSpec(
    name="create_schedule",
    description=(
        "Create a recurring schedule that wakes this thread with a prompt. "
        "`spec` is 5-field cron (e.g. '0 9 * * *') or '@every <seconds>'. "
        "Fires landing in quiet hours are deferred to the window's end."
    ),
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "spec": {"type": "string"},
            "prompt": {
                "type": "string",
                "description": "what to do each time it fires",
            },
        },
        "required": ["description", "spec", "prompt"],
    },
)

_LIST_SPEC = ToolSpec(
    name="list_schedules",
    description="List all active schedules.",
    parameters={"type": "object", "properties": {}},
)

_DELETE_SPEC = ToolSpec(
    name="delete_schedule",
    description="Delete a schedule by id.",
    parameters={
        "type": "object",
        "properties": {"schedule_id": {"type": "integer"}},
        "required": ["schedule_id"],
    },
)


def register_cron_tools(registry: ToolRegistry, service: CronService) -> None:
    """Expose create/list/delete schedule tools backed by the service."""

    async def create_schedule(
        description: str,
        spec: str,
        prompt: str,
        context: ToolContext | None = None,
    ) -> str:
        if context is None:
            return "error: create_schedule needs a session context"
        schedule_id = await service.create(
            description=description,
            spec=spec,
            wake_channel=context.channel,
            wake_thread=context.thread_key,
            prompt=prompt,
        )
        return f"schedule #{schedule_id} created"

    async def list_schedules() -> str:
        rows = await service.list_enabled()
        if not rows:
            return "no schedules"
        return "\n".join(
            f"#{r.id} [{r.spec}] {r.description} -> wakes {r.wake_thread}"
            for r in rows
        )

    async def delete_schedule(schedule_id: int) -> str:
        deleted = await service.delete(schedule_id)
        return (
            f"schedule #{schedule_id} deleted" if deleted else "error: no such schedule"
        )

    registry.register(Tool(_CREATE_SPEC, create_schedule, wants_context=True))
    registry.register(Tool(_LIST_SPEC, list_schedules))
    registry.register(Tool(_DELETE_SPEC, delete_schedule))
