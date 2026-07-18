"""Native tool for the agent to manage its own monitors."""

from typing import Any

from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.monitors.service import MonitorService
from chief.provider.base import ToolSpec

_SPEC = ToolSpec(
    name="monitor",
    description=(
        "Manage monitors that watch a channel and wake this thread on a match. "
        "action=create needs `description` and exactly one of three forms: "
        "`pattern` (regex over event text, cheap), `instruction` (a "
        "yes/no judgment via the built-in wake-judge classifier), or "
        "`classifier` (a named categorical classifier, which requires "
        "`fire_label` — the label that fires the monitor). Plus optional "
        "`watch_channel` (defaults to this thread's channel). "
        "action=list takes nothing. action=delete needs `monitor_id`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "list", "delete"]},
            "description": {"type": "string"},
            "pattern": {"type": "string"},
            "instruction": {"type": "string"},
            "classifier": {"type": "string"},
            "fire_label": {"type": "string"},
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
        classifier: str | None,
        fire_label: str | None,
        watch_channel: str | None,
    ) -> str:
        if context is None:
            return "error: create needs a session context"
        if not description:
            return "error: create needs a description"
        forms = [pattern, instruction, classifier]
        if sum(form is not None for form in forms) != 1:
            return "error: give exactly one of pattern, instruction, or classifier"
        if classifier is not None and not fire_label:
            return "error: classifier needs a fire_label"
        predicate: dict[str, Any]
        if pattern is not None:
            predicate = {"kind": "code", "field": "text", "pattern": pattern}
        elif instruction is not None:
            predicate = {
                "kind": "classifier",
                "classifier": "wake-judge",
                "fire_label": "YES",
                "instruction": instruction,
            }
        else:
            predicate = {
                "kind": "classifier",
                "classifier": classifier,
                "fire_label": fire_label,
            }
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
        classifier: str | None = None,
        fire_label: str | None = None,
        watch_channel: str | None = None,
        monitor_id: Any = None,
    ) -> str:
        if action == "create":
            return await _create(
                context,
                description,
                pattern,
                instruction,
                classifier,
                fire_label,
                watch_channel,
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
