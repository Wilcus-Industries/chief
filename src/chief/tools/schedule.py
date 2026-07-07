"""chief's own scheduling tools (M9a) — the owner sets up unprompted, future work.

Two SDK MCP services on **separate servers**, split for the same gate-isolation reason
as :mod:`chief.tools.guest`:

- :class:`ScheduleService` (server ``chief_schedule``) — the benign tools
  (``schedule_once``, ``schedule_recurring``, ``create_monitor``, ``list_schedules``,
  ``cancel_schedule``). These can only mint ``message`` and ``wakeup`` actions, and a
  benign monitor judges its predicate with a read-only Haiku (never a shell command). A
  ``message`` fire just sends text (no model, harmless); a ``wakeup`` fire boots a full
  agent turn that re-passes the permission gate, so any effectful tool it reaches still
  raises an approval card. All are safe to create off the allow-list — no card needed.
- :class:`ScheduleBashService` (server ``chief_schedule_bash``) — ``schedule_bash`` and
  a gated ``create_monitor``, which mint a ``bash`` action and/or ``bash`` predicate. A
  ``bash`` fire runs a host shell command with no agent and **no per-fire gate**, so
  creating one is itself the gated act: these tools are left off the owner's allow-list
  and raise an approval card at creation time. The benign tools refuse
  ``action_type="bash"`` (and a bash predicate), so this is the only path to one.

A monitor (``kind="monitor"``) reuses ``spec`` as a cron *cadence*: each due tick checks
its predicate and fires its action only on a false→true flip. A ``monitor_min_interval``
floor (seconds between checks) keeps an agent monitor from polling every tick.

Times entered by the owner are read in ``owner_tz``: a naive ISO timestamp or a cron
expression is local wall-clock, converted to the UTC stored on the row. A spec that
won't parse, or that resolves to the past, is rejected up front rather than stored to
fail at fire time.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    McpSdkServerConfig,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.schedule_time import next_fire
from ..persistence.models import Schedule
from ..persistence.schedules import (
    ACTION_BASH,
    ACTION_MESSAGE,
    ACTION_WAKEUP,
    KIND_MONITOR,
    KIND_ONCE,
    KIND_RECURRING,
    PREDICATE_AGENT,
    PREDICATE_BASH,
    create_schedule,
    disable_schedule,
    get_schedule,
    list_enabled,
)

#: Action types the benign server may create — never ``bash`` (that needs the gate).
_BENIGN_ACTION_TYPES = (ACTION_MESSAGE, ACTION_WAKEUP)
#: Every action type the gated bash server may mint (adds the ungated ``bash`` fire).
_BASH_ACTION_TYPES = (ACTION_MESSAGE, ACTION_WAKEUP, ACTION_BASH)
#: Predicate types a monitor may watch with.
_PREDICATE_TYPES = (PREDICATE_BASH, PREDICATE_AGENT)

_ONCE_DESCRIPTION = (
    "Schedule a one-off action at a future time. `when` is an ISO-8601 timestamp "
    "(e.g. 2026-06-05T09:00:00); a value without a timezone is read in the owner's "
    "local time. `action_type` is 'message' (default) to send `action` as plain text "
    "— free, no thinking — or 'wakeup' to boot a full agent turn with `action` as the "
    "prompt. Optional `thread_key` targets a specific conversation (default: the "
    "owner's primary inbox); set `urgent` to let it fire during quiet hours."
)

_RECURRING_DESCRIPTION = (
    "Schedule a repeating action on a cron expression. `cron` is a 5-field expression "
    "(e.g. '0 9 * * *' for 9am daily) read in the owner's local time. `action_type` is "
    "'message' (default) to send `action` as plain text, or 'wakeup' to boot a full "
    "agent turn with `action` as the prompt. Optional `thread_key` targets a specific "
    "conversation (default: the owner's primary inbox); set `urgent` to let it fire "
    "during quiet hours."
)

_LIST_DESCRIPTION = (
    "List every active schedule — reminders, recurring jobs, and scheduled commands — "
    "with its id and next fire time. Use the id with cancel_schedule."
)

_CANCEL_DESCRIPTION = "Cancel an active schedule by its id (from list_schedules)."

_BASH_DESCRIPTION = (
    "Schedule a shell command to run unattended on the host. Give the `command` and "
    "exactly one of `cron` (a 5-field cron expression, recurring) or `when` (an "
    "ISO-8601 timestamp, one-off), read in the owner's local time. Output is delivered "
    "to `thread_key` (default: the primary inbox), as a file if long. Set `urgent` to "
    "let it fire during quiet hours. This sets up an ungated command run, so it needs "
    "the owner's approval now."
)

_MONITOR_DESCRIPTION = (
    "Set up a monitor: check a condition on a schedule and notify only when it first "
    "becomes true (the false→true flip), not on every check. `cadence` is a 5-field "
    "cron expression (e.g. '*/15 * * * *' for every 15 min) read in the owner's local "
    "time. `predicate` is the condition to watch, judged YES/NO by a quick read-only "
    "model that can look things up (web + Google reads). On the flip, `action_type` "
    "is 'message' (default) to send `action` as text, or 'wakeup' to boot a full agent "
    "turn with `action` as the prompt. Optional `thread_key` targets a conversation "
    "(default: the primary inbox); set `urgent` to let it fire during quiet hours."
)

_MONITOR_BASH_DESCRIPTION = (
    "Set up a monitor whose predicate or action uses the host shell. `cadence` is a "
    "5-field cron expression read in the owner's local time. `predicate` is the "
    "condition to watch and `predicate_type` is 'bash' (a shell command — exit 0 means "
    "true) or 'agent' (a read-only model yes/no). On the false→true flip, `action` "
    "fires; `action_type` is 'message' (default), 'wakeup', or 'bash' (run `action` as "
    "a shell command). A bash predicate or bash action runs ungated on every check or "
    "flip, so setting one up needs the owner's approval now."
)

_TARGET_DESCRIPTION = (
    "Conversation to deliver to, as 'chat_id:thread_id'. Defaults to the primary inbox."
)
_URGENT_DESCRIPTION = "Allow firing during the owner's quiet hours."
_CADENCE_DESCRIPTION = "5-field cron expression — how often to check the condition."
_PREDICATE_DESCRIPTION = "The condition to watch for."

_MONITOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cadence": {"type": "string", "description": _CADENCE_DESCRIPTION},
        "predicate": {"type": "string", "description": _PREDICATE_DESCRIPTION},
        "action": {"type": "string", "description": "Text to send, or wakeup prompt."},
        "action_type": {
            "type": "string",
            "enum": list(_BENIGN_ACTION_TYPES),
            "description": "'message' (default) or 'wakeup'.",
        },
        "thread_key": {"type": "string", "description": _TARGET_DESCRIPTION},
        "urgent": {"type": "boolean", "description": _URGENT_DESCRIPTION},
    },
    "required": ["cadence", "predicate", "action"],
}

_MONITOR_BASH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cadence": {"type": "string", "description": _CADENCE_DESCRIPTION},
        "predicate": {
            "type": "string",
            "description": "Condition: a shell command (bash) or a question (agent).",
        },
        "predicate_type": {
            "type": "string",
            "enum": list(_PREDICATE_TYPES),
            "description": "'bash' (exit 0 = true, default) or 'agent'.",
        },
        "action": {"type": "string", "description": "Text, prompt, or shell command."},
        "action_type": {
            "type": "string",
            "enum": list(_BASH_ACTION_TYPES),
            "description": "'message' (default), 'wakeup', or 'bash'.",
        },
        "thread_key": {"type": "string", "description": _TARGET_DESCRIPTION},
        "urgent": {"type": "boolean", "description": _URGENT_DESCRIPTION},
    },
    "required": ["cadence", "predicate", "action"],
}

_ONCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "when": {"type": "string", "description": "ISO-8601 timestamp to fire at."},
        "action": {"type": "string", "description": "Text to send, or wakeup prompt."},
        "action_type": {
            "type": "string",
            "enum": list(_BENIGN_ACTION_TYPES),
            "description": "'message' (default) or 'wakeup'.",
        },
        "thread_key": {"type": "string", "description": _TARGET_DESCRIPTION},
        "urgent": {"type": "boolean", "description": _URGENT_DESCRIPTION},
    },
    "required": ["when", "action"],
}

_RECURRING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cron": {"type": "string", "description": "5-field cron expression."},
        "action": {"type": "string", "description": "Text to send, or wakeup prompt."},
        "action_type": {
            "type": "string",
            "enum": list(_BENIGN_ACTION_TYPES),
            "description": "'message' (default) or 'wakeup'.",
        },
        "thread_key": {"type": "string", "description": _TARGET_DESCRIPTION},
        "urgent": {"type": "boolean", "description": _URGENT_DESCRIPTION},
    },
    "required": ["cron", "action"],
}

_BASH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Shell command to run."},
        "cron": {"type": "string", "description": "5-field cron, recurring."},
        "when": {"type": "string", "description": "ISO-8601 timestamp (one-off)."},
        "thread_key": {"type": "string", "description": _TARGET_DESCRIPTION},
        "urgent": {"type": "boolean", "description": _URGENT_DESCRIPTION},
    },
    "required": ["command"],
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


def _opt(args: dict[str, Any], key: str) -> str | None:
    """A trimmed optional string arg, or ``None`` when absent/blank."""
    value = args.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fmt_local(dt: datetime | None, tz: tzinfo) -> str:
    """Format a stored (UTC) timestamp in the owner's timezone, or ``"—"``."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:  # sqlite hands back naive UTC
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _confirm(sched: Schedule, tz: tzinfo) -> str:
    return (
        f"✅ Scheduled #{sched.id} ({sched.action_type}) — "
        f"next fire {_fmt_local(sched.next_run, tz)}."
    )


def _describe(sched: Schedule, tz: tzinfo) -> str:
    nxt = _fmt_local(sched.next_run, tz)
    if sched.kind == KIND_MONITOR:
        return (
            f"#{sched.id} monitor [{sched.predicate_type}] {sched.predicate} "
            f"→ [{sched.action_type}] {sched.action} "
            f"— every {sched.spec}, next check {nxt}"
        )
    trigger = sched.spec if sched.kind == KIND_RECURRING else "once"
    return f"#{sched.id} [{sched.action_type}] {sched.action} — {trigger}, next {nxt}"


def _cadence_too_fast(
    cron: str, now: datetime, tz: tzinfo, min_seconds: int
) -> bool:
    """True if ``cron`` would check more often than ``min_seconds`` apart.

    Compares the first two future occurrences (an unparseable cron returns ``False`` —
    :func:`_store` rejects it with a clearer message). Guards an agent monitor from
    polling every tick and burning Haiku budget.
    """
    first = next_fire(KIND_MONITOR, cron, after=now, tz=tz)
    if first is None:
        return False
    second = next_fire(KIND_MONITOR, cron, after=first, tz=tz)
    if second is None:
        return False
    return (second - first).total_seconds() < min_seconds


async def _store(
    session_factory: async_sessionmaker[AsyncSession],
    tz: tzinfo,
    now: datetime,
    *,
    kind: str,
    spec: str,
    action: str,
    action_type: str,
    thread_key: str | None,
    urgent: bool,
    predicate: str | None = None,
    predicate_type: str | None = None,
) -> tuple[Schedule | None, str]:
    """Validate the spec and create the row, or return ``(None, error message)``.

    A spec that won't parse (:func:`next_fire` raises or returns ``None``) or that
    resolves to the past is rejected here, so the stored row always has a future
    ``next_run`` the scheduler can act on. ``predicate`` / ``predicate_type`` are passed
    through for ``monitor`` rows.
    """
    try:
        nxt = next_fire(kind, spec, after=now, tz=tz)
    except ValueError:
        nxt = None
    if nxt is None:
        problem = (
            "Couldn't read that as a date/time (use ISO-8601, e.g. 2026-06-05T09:00)."
            if kind == KIND_ONCE
            else "Couldn't read that as a cron expression (e.g. '0 9 * * *')."
        )
        return None, problem
    if nxt <= now:
        return None, f"That time ({_fmt_local(nxt, tz)}) is already in the past."
    async with session_factory() as session:
        sched = await create_schedule(
            session,
            kind=kind,
            spec=spec,
            action=action,
            action_type=action_type,
            next_run=nxt,
            thread_key=thread_key,
            urgent=urgent,
            predicate=predicate,
            predicate_type=predicate_type,
        )
    return sched, ""


async def _store_monitor(
    session_factory: async_sessionmaker[AsyncSession],
    tz: tzinfo,
    now: datetime,
    min_interval_seconds: int,
    *,
    cadence: str,
    predicate: str,
    predicate_type: str,
    action: str,
    action_type: str,
    thread_key: str | None,
    urgent: bool,
) -> dict[str, Any]:
    """Shared body for ``create_monitor`` (both servers): validate, then store."""
    if not predicate:
        return _text_result("Nothing to watch (empty predicate).", is_error=True)
    if not action:
        return _text_result("Nothing to do on the flip (empty action).", is_error=True)
    if _cadence_too_fast(cadence, now, tz, min_interval_seconds):
        return _text_result(
            f"That cadence checks more often than every {min_interval_seconds}s — "
            "widen it so the monitor doesn't poll too aggressively.",
            is_error=True,
        )
    sched, err = await _store(
        session_factory,
        tz,
        now,
        kind=KIND_MONITOR,
        spec=cadence,
        action=action,
        action_type=action_type,
        thread_key=thread_key,
        urgent=urgent,
        predicate=predicate,
        predicate_type=predicate_type,
    )
    if sched is None:
        return _text_result(err, is_error=True)
    return _text_result(_confirm(sched, tz))


async def _store_benign(
    session_factory: async_sessionmaker[AsyncSession],
    tz: tzinfo,
    now: datetime,
    *,
    kind: str,
    spec: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Shared body for ``schedule_once`` / ``schedule_recurring`` (message + wakeup)."""
    action = str(args.get("action", "")).strip()
    if not action:
        return _text_result("Nothing to schedule (empty action).", is_error=True)
    action_type = str(args.get("action_type") or ACTION_MESSAGE)
    if action_type not in _BENIGN_ACTION_TYPES:
        return _text_result(
            f"This tool can't create {action_type!r} schedules. "
            "Use the schedule_bash tool to schedule a shell command.",
            is_error=True,
        )
    sched, err = await _store(
        session_factory,
        tz,
        now,
        kind=kind,
        spec=spec,
        action=action,
        action_type=action_type,
        thread_key=_opt(args, "thread_key"),
        urgent=bool(args.get("urgent", False)),
    )
    if sched is None:
        return _text_result(err, is_error=True)
    return _text_result(_confirm(sched, tz))


@dataclass(frozen=True)
class ScheduleService:
    """The owner's benign schedule tools (message + wakeup, plus list/cancel)."""

    session_factory: async_sessionmaker[AsyncSession]
    owner_tz: str = "UTC"
    now: Callable[[], datetime] = _utcnow
    server_name: str = "chief_schedule"
    monitor_min_interval_seconds: int = 300

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The SDK-qualified benign tool names — added to the owner's allow-list."""
        n = self.server_name
        return (
            f"mcp__{n}__schedule_once",
            f"mcp__{n}__schedule_recurring",
            f"mcp__{n}__create_monitor",
            f"mcp__{n}__list_schedules",
            f"mcp__{n}__cancel_schedule",
        )

    def _tz(self) -> tzinfo:
        return ZoneInfo(self.owner_tz)

    def _build_once(self) -> SdkMcpTool[Any]:
        factory, tz, now_fn = self.session_factory, self._tz(), self.now

        @tool("schedule_once", _ONCE_DESCRIPTION, _ONCE_SCHEMA)
        async def schedule_once(args: dict[str, Any]) -> dict[str, Any]:
            return await _store_benign(
                factory, tz, now_fn(), kind=KIND_ONCE,
                spec=str(args.get("when", "")), args=args,
            )

        return schedule_once

    def _build_recurring(self) -> SdkMcpTool[Any]:
        factory, tz, now_fn = self.session_factory, self._tz(), self.now

        @tool("schedule_recurring", _RECURRING_DESCRIPTION, _RECURRING_SCHEMA)
        async def schedule_recurring(args: dict[str, Any]) -> dict[str, Any]:
            return await _store_benign(
                factory, tz, now_fn(), kind=KIND_RECURRING,
                spec=str(args.get("cron", "")), args=args,
            )

        return schedule_recurring

    def _build_monitor(self) -> SdkMcpTool[Any]:
        factory, tz, now_fn = self.session_factory, self._tz(), self.now
        min_interval = self.monitor_min_interval_seconds

        @tool("create_monitor", _MONITOR_DESCRIPTION, _MONITOR_SCHEMA)
        async def create_monitor(args: dict[str, Any]) -> dict[str, Any]:
            action_type = str(args.get("action_type") or ACTION_MESSAGE)
            if action_type not in _BENIGN_ACTION_TYPES:
                return _text_result(
                    f"This tool can't create {action_type!r} monitors. Use the "
                    "gated create_monitor for a shell predicate or action.",
                    is_error=True,
                )
            return await _store_monitor(
                factory, tz, now_fn(), min_interval,
                cadence=str(args.get("cadence", "")),
                predicate=str(args.get("predicate", "")).strip(),
                predicate_type=PREDICATE_AGENT,  # benign = read-only Haiku judgment
                action=str(args.get("action", "")).strip(),
                action_type=action_type,
                thread_key=_opt(args, "thread_key"),
                urgent=bool(args.get("urgent", False)),
            )

        return create_monitor

    def _build_list(self) -> SdkMcpTool[Any]:
        factory, tz = self.session_factory, self._tz()

        @tool("list_schedules", _LIST_DESCRIPTION, {})
        async def list_schedules(args: dict[str, Any]) -> dict[str, Any]:
            async with factory() as session:
                rows = await list_enabled(session)
            if not rows:
                return _text_result("No active schedules.")
            return _text_result("\n".join(_describe(r, tz) for r in rows))

        return list_schedules

    def _build_cancel(self) -> SdkMcpTool[Any]:
        factory = self.session_factory

        @tool("cancel_schedule", _CANCEL_DESCRIPTION, {"schedule_id": int})
        async def cancel_schedule(args: dict[str, Any]) -> dict[str, Any]:
            sid = int(args["schedule_id"])
            async with factory() as session:
                sched = await get_schedule(session, sid)
                if sched is None:
                    return _text_result(f"No schedule #{sid}.", is_error=True)
                if not sched.enabled:
                    return _text_result(f"#{sid} is already cancelled.")
                await disable_schedule(session, sched)
            return _text_result(f"Cancelled #{sid}.")

        return cancel_schedule

    def server_config(self) -> McpSdkServerConfig:
        """The in-process ``mcp_servers`` entry for the benign schedule tools."""
        return create_sdk_mcp_server(
            self.server_name,
            tools=[
                self._build_once(),
                self._build_recurring(),
                self._build_monitor(),
                self._build_list(),
                self._build_cancel(),
            ],
        )


@dataclass(frozen=True)
class ScheduleBashService:
    """The owner's gated ``schedule_bash`` tool (mints an ungated shell command)."""

    session_factory: async_sessionmaker[AsyncSession]
    owner_tz: str = "UTC"
    now: Callable[[], datetime] = _utcnow
    server_name: str = "chief_schedule_bash"
    monitor_min_interval_seconds: int = 300

    @property
    def tool_name(self) -> str:
        """The ``schedule_bash`` name — deliberately kept off the owner's allow-list."""
        return f"mcp__{self.server_name}__schedule_bash"

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Both gated names (``schedule_bash`` + ``create_monitor``): off allow-list.

        Kept off ``allowed_tools`` so each mint routes through ``can_use_tool`` → the
        owner's approval card (setting up an ungated shell run is itself the gated act).
        """
        n = self.server_name
        return (f"mcp__{n}__schedule_bash", f"mcp__{n}__create_monitor")

    def _tz(self) -> tzinfo:
        return ZoneInfo(self.owner_tz)

    def _build_tool(self) -> SdkMcpTool[Any]:
        factory, tz, now_fn = self.session_factory, self._tz(), self.now

        @tool("schedule_bash", _BASH_DESCRIPTION, _BASH_SCHEMA)
        async def schedule_bash(args: dict[str, Any]) -> dict[str, Any]:
            command = str(args.get("command", "")).strip()
            if not command:
                return _text_result("No command to run.", is_error=True)
            cron, when = _opt(args, "cron"), _opt(args, "when")
            if bool(cron) == bool(when):
                return _text_result(
                    "Give exactly one of `cron` (recurring) or `when` (one-off).",
                    is_error=True,
                )
            kind = KIND_RECURRING if cron else KIND_ONCE
            sched, err = await _store(
                factory, tz, now_fn(),
                kind=kind, spec=cron or when or "", action=command,
                action_type=ACTION_BASH,
                thread_key=_opt(args, "thread_key"),
                urgent=bool(args.get("urgent", False)),
            )
            if sched is None:
                return _text_result(err, is_error=True)
            return _text_result(_confirm(sched, tz))

        return schedule_bash

    def _build_monitor(self) -> SdkMcpTool[Any]:
        factory, tz, now_fn = self.session_factory, self._tz(), self.now
        min_interval = self.monitor_min_interval_seconds

        @tool("create_monitor", _MONITOR_BASH_DESCRIPTION, _MONITOR_BASH_SCHEMA)
        async def create_monitor(args: dict[str, Any]) -> dict[str, Any]:
            predicate_type = str(args.get("predicate_type") or PREDICATE_BASH)
            if predicate_type not in _PREDICATE_TYPES:
                return _text_result(
                    f"Unknown predicate_type {predicate_type!r} (use bash or agent).",
                    is_error=True,
                )
            action_type = str(args.get("action_type") or ACTION_MESSAGE)
            if action_type not in _BASH_ACTION_TYPES:
                return _text_result(
                    f"Unknown action_type {action_type!r} "
                    "(use 'message', 'wakeup', or 'bash').",
                    is_error=True,
                )
            return await _store_monitor(
                factory, tz, now_fn(), min_interval,
                cadence=str(args.get("cadence", "")),
                predicate=str(args.get("predicate", "")).strip(),
                predicate_type=predicate_type,
                action=str(args.get("action", "")).strip(),
                action_type=action_type,
                thread_key=_opt(args, "thread_key"),
                urgent=bool(args.get("urgent", False)),
            )

        return create_monitor

    def server_config(self) -> McpSdkServerConfig:
        """The in-process ``mcp_servers`` entry for the gated bash-schedule tools."""
        return create_sdk_mcp_server(
            self.server_name, tools=[self._build_tool(), self._build_monitor()]
        )
