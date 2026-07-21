"""Native tool for the agent to manage its own monitors."""

from typing import Any

from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.monitors.predicate import MATCHABLE_FIELDS, build_predicate
from chief.monitors.service import MonitorService
from chief.provider.base import ToolSpec

_SPEC = ToolSpec(
    name="monitor",
    description=(
        "Manage monitors that watch a channel and wake this thread on a match. "
        "action=create needs `description` and exactly one of three forms: "
        "`pattern` (regex over ONE field of the event, cheap — `field` picks "
        f"which, one of {', '.join(MATCHABLE_FIELDS)}, default `text`; note "
        "`text` is the bare message body and never includes the sender, so "
        "match a contact with field=`sender`), `instruction` (a "
        "yes/no judgment via the built-in wake-judge classifier), or "
        "`classifier` (a named categorical classifier, which requires "
        "`fire_label` — the label that fires the monitor). Plus optional "
        "`watch_channel` (defaults to this thread's channel) and "
        "`target_session` (an existing session's thread_key; which session the "
        "wake runs in, default this thread). "
        "action=list takes nothing. action=delete needs `monitor_id`. "
        "action=retarget needs `monitor_id` and re-points the wake to "
        "`target_session` (default this thread)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "delete", "retarget"],
            },
            "description": {"type": "string"},
            "pattern": {"type": "string"},
            "field": {
                "type": "string",
                "enum": list(MATCHABLE_FIELDS),
                "description": "event field `pattern` matches; default text",
            },
            "instruction": {"type": "string"},
            "classifier": {"type": "string"},
            "fire_label": {"type": "string"},
            "watch_channel": {
                "type": "string",
                "description": "channel to watch; defaults to this thread's channel",
            },
            "target_session": {
                "type": "string",
                "description": (
                    "thread_key of an existing session to wake; defaults to "
                    "this thread. Must already exist (see the session tool)."
                ),
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
        field: str | None,
        target_session: str | None,
    ) -> str:
        if context is None:
            return "error: create needs a session context"
        if not description:
            return "error: create needs a description"
        predicate = build_predicate(
            pattern, instruction, classifier, fire_label, field,
            service.classifier_def,
        )
        if isinstance(predicate, str):
            return predicate  # a validation error
        resolved = await service.store.resolve_wake_target(
            target_session, context.channel, context.thread_key
        )
        if resolved is None:
            return f"error: no such session '{target_session}'"
        wake_channel, wake_thread = resolved
        monitor_id = await service.create(
            description=description,
            watch_channel=watch_channel or context.channel,
            wake_channel=wake_channel,
            wake_thread=wake_thread,
            predicate=predicate,
        )
        return f"monitor #{monitor_id} created"

    async def _retarget(
        context: ToolContext | None,
        monitor_id: Any,
        target_session: str | None,
    ) -> str:
        if context is None:
            return "error: retarget needs a session context"
        if not isinstance(monitor_id, int):
            return "error: retarget needs monitor_id"
        status, wake_thread = await service.retarget(
            monitor_id, target_session, context.channel, context.thread_key
        )
        if status == "missing":
            return "error: no such monitor"
        if status == "unknown-target":
            return f"error: no such session '{target_session}'"
        return f"monitor #{monitor_id} now wakes {wake_thread}"

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
        field: str | None = None,
        target_session: str | None = None,
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
                field,
                target_session,
            )
        if action == "retarget":
            return await _retarget(context, monitor_id, target_session)
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
