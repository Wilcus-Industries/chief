"""Native tool for the agent to manage its own monitors."""

from typing import Any

from chief.monitors.predicate import MATCHABLE_FIELDS, build_predicate, scope_values
from chief.monitors.service import MonitorService
from chief.persistence.models import MonitorRow
from chief.provider.base import ToolSpec
from chief.tools import Tool, ToolContext, ToolRegistry

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
        "`fire_label` — the label that fires the monitor). The `instruction` "
        "and `classifier` forms send message text to a model, so each MUST be "
        "scoped to one contact (`scope_sender`) or one thread/group chat "
        "(`scope_thread`) — ask the owner whose messages it may read, never "
        "guess. `pattern` is local regex and takes no scope. Plus optional "
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
            "scope_sender": {
                "type": "string",
                "description": (
                    "the one sender an instruction/classifier monitor may "
                    "read. Must equal the event's `sender` exactly (case "
                    "aside): the handle as it appears in events, e.g. "
                    "+16505551212, never 650-555-1212"
                ),
            },
            "scope_thread": {
                "type": "string",
                "description": (
                    "the one thread_key an instruction/classifier monitor may "
                    "read (e.g. a group chat id), matched exactly"
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
        scope_sender: str | None,
        scope_thread: str | None,
    ) -> str:
        if context is None:
            return "error: create needs a session context"
        if not description:
            return "error: create needs a description"
        predicate = build_predicate(
            pattern, instruction, classifier, fire_label, field,
            service.classifier_def, scope_sender, scope_thread,
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

    def _describe(row: MonitorRow) -> str:
        # Scope and disabled state both show: a boot sweep can disable a row
        # (#285), and an invisible one is one the owner never rescopes.
        scope = scope_values(row.predicate.get("scope"))
        suffix = "".join(f" scope={f}:{v}" for f, v in scope.items())
        if not row.enabled:
            suffix += " [disabled: unscoped classifier — recreate it scoped]"
        return (
            f"#{row.id} [{row.watch_channel}] {row.description} "
            f"-> wakes {row.wake_thread}{suffix}"
        )

    async def _list() -> str:
        rows = await service.list_monitors(include_disabled=True)
        return "\n".join(_describe(r) for r in rows) if rows else "no monitors"

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
        scope_sender: str | None = None,
        scope_thread: str | None = None,
    ) -> str:
        if action == "create":
            return await _create(
                context, description, pattern, instruction, classifier,
                fire_label, watch_channel, field, target_session,
                scope_sender, scope_thread,
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
