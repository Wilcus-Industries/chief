"""Native tool for the agent to manage its own schedules."""

from typing import Any

from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.cron.service import CronService
from chief.provider.base import ToolSpec

_SPEC = ToolSpec(
    name="schedule",
    description=(
        "Manage recurring schedules that wake this thread with a prompt. "
        "action=create needs `description`, `spec` (5-field cron like "
        "'0 9 * * *' or '@every <seconds>'), and `prompt` (what to do each "
        "fire); fires landing in quiet hours defer to the window's end. "
        "action=list takes nothing. action=delete needs `schedule_id`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "list", "delete"]},
            "description": {"type": "string"},
            "spec": {"type": "string"},
            "prompt": {
                "type": "string",
                "description": "what to do each time it fires",
            },
            "schedule_id": {"type": "integer"},
        },
        "required": ["action"],
    },
)


def register_cron_tools(registry: ToolRegistry, service: CronService) -> None:
    """Expose the schedule tool (create/list/delete) backed by the service."""

    async def _create(
        context: ToolContext | None,
        description: str | None,
        spec: str | None,
        prompt: str | None,
    ) -> str:
        if context is None:
            return "error: create needs a session context"
        if not (description and spec and prompt):
            return "error: create needs description, spec, and prompt"
        schedule_id = await service.create(
            description=description,
            spec=spec,
            wake_channel=context.channel,
            wake_thread=context.thread_key,
            prompt=prompt,
        )
        return f"schedule #{schedule_id} created"

    async def _list() -> str:
        rows = await service.list_enabled()
        if not rows:
            return "no schedules"
        return "\n".join(
            f"#{r.id} [{r.spec}] {r.description} -> wakes {r.wake_thread}"
            for r in rows
        )

    async def schedule(
        action: str,
        context: ToolContext | None = None,
        description: str | None = None,
        spec: str | None = None,
        prompt: str | None = None,
        schedule_id: Any = None,
    ) -> str:
        if action == "create":
            return await _create(context, description, spec, prompt)
        if action == "list":
            return await _list()
        if action == "delete":
            if not isinstance(schedule_id, int):
                return "error: delete needs schedule_id"
            deleted = await service.delete(schedule_id)
            return (
                f"schedule #{schedule_id} deleted"
                if deleted
                else "error: no such schedule"
            )
        return f"error: unknown action '{action}'"

    registry.register(Tool(_SPEC, schedule, wants_context=True))
