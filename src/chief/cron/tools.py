"""Native tool for the agent to manage its own schedules."""

import json
from typing import Any

from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.approvals import Approval
from chief.cron.service import CronService
from chief.cron.timing import validate_spec
from chief.gate import AskApproval
from chief.persistence.models import ScheduleRow
from chief.provider.base import ToolSpec

_SPEC = ToolSpec(
    name="schedule",
    description=(
        "Manage recurring schedules. action=create needs `description`, "
        "`spec` (5-field cron like '0 9 * * *' or '@every <seconds>'), and "
        "exactly one of `prompt` (wakes this thread to do it, deferring out "
        "of quiet hours) or `command` (runs a shell command unattended — no "
        "model turn, no approval at fire time, so creating one asks the "
        "owner first and ignores quiet hours). action=list takes nothing. "
        "action=delete needs `schedule_id`."
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
            "command": {
                "type": "string",
                "description": (
                    "a shell command to run unattended instead of waking the "
                    "thread; creating one always asks the owner first"
                ),
            },
            "schedule_id": {"type": "integer"},
        },
        "required": ["action"],
    },
)


def _row_line(row: ScheduleRow) -> str:
    target = f"runs `{row.command}`" if row.command else f"wakes {row.wake_thread}"
    return f"#{row.id} [{row.spec}] {row.description} -> {target}"


def register_cron_tools(
    registry: ToolRegistry, service: CronService, ask: AskApproval | None = None
) -> None:
    """Expose the schedule tool (create/list/delete) backed by the service.

    ``ask`` is the approval-card path; without it, command schedules cannot be
    created at all.
    """

    async def _approve_command(context: ToolContext, spec: str, command: str) -> bool:
        """Ask the owner before a command schedule exists.

        A scheduled command runs with nobody present, so creation is the only
        control point. No asker wired means no way to ask — refuse, rather than
        let a missing control read as permission.
        """
        if ask is None:
            return False
        # json.dumps escapes newlines, so neither field can draw its own
        # "yes / no" line and bury the real payload above or below it. This card
        # is the only control point for unattended shell execution — it must not
        # be forgeable by anything it is asking about.
        question = (
            f"approve a scheduled command on {json.dumps(spec)}? it will run "
            "unattended, with no approval when it fires:\n"
            f"{json.dumps(command)}\nyes / no"
        )
        # ALWAYS has nothing to persist here — treat it as this one yes.
        return await ask(context, question) is not Approval.DENY

    async def _create(
        context: ToolContext | None,
        description: str | None,
        spec: str | None,
        prompt: str | None,
        command: str | None,
    ) -> str:
        if context is None:
            return "error: create needs a session context"
        if not (description and spec) or bool(prompt) == bool(command):
            return (
                "error: create needs description, spec, and exactly one of "
                "prompt or command"
            )
        try:
            validate_spec(spec)
        except ValueError as exc:
            # Reject before the card: an unparsable spec would otherwise both
            # forge the card and wedge the schedule loop once persisted.
            return f"error: {exc}"
        if command and not await _approve_command(context, spec, command):
            return "schedule not created: the owner declined the command"
        schedule_id = await service.create(
            description=description,
            spec=spec,
            wake_channel=context.channel,
            wake_thread=context.thread_key,
            prompt=prompt or "",
            command=command,
        )
        return f"schedule #{schedule_id} created"

    async def _list() -> str:
        rows = await service.list_enabled()
        if not rows:
            return "no schedules"
        return "\n".join(_row_line(r) for r in rows)

    async def schedule(
        action: str,
        context: ToolContext | None = None,
        description: str | None = None,
        spec: str | None = None,
        prompt: str | None = None,
        command: str | None = None,
        schedule_id: Any = None,
    ) -> str:
        if action == "create":
            return await _create(context, description, spec, prompt, command)
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
