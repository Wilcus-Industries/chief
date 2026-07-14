"""chief's watch tools (#165, part of PRD #160) — standing owner instructions.

One in-process server (``chief_watches``), owner-only, wired into owner sessions
whenever the iMessage adapter is configured. Three tools:

- ``create_watch`` — record a standing instruction over a Contacts-resolved
  handle ("if mom texts me today about x, tell her y"). Contact resolution is
  NOT this tool's job: the owner session already has ``lookup_contact``
  (:mod:`chief.tools.apple.contacts`), so the model resolves "mom" to a handle
  with that tool first, then calls ``create_watch`` with the resolved handle.
  Likewise "today" → local midnight and "execute silently" → ``tone="silent"``
  are natural-language judgments the model makes, exactly like
  ``schedule_once``'s ``when`` field — this tool only validates/stores what
  it's given. ``expiry`` reuses :func:`chief.core.schedule_time.next_fire`
  (the same ISO/timezone parser ``schedule_once`` uses); omitting it applies
  the 14-day default (:func:`chief.persistence.watches.default_expiry`).
- ``list_watches`` — every watch with its effective state (an armed watch past
  its expiry reads as ``expired``, per
  :func:`chief.persistence.watches.effective_state`).
- ``cancel_watch`` — flip an armed watch to ``cancelled`` for good; a cancelled
  or already-expired watch can never fire, and cancelling twice is a no-op.

All three are owner-initiated and reversible-enough (cancel always undoes a watch;
a wrong-target create is caught by the confirmation echo) → pre-approved, no card,
the same posture as :class:`~chief.tools.imessage_admin.IMessageAdminService`.

Nothing here fires a watch — that's a later milestone (PRD #160).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.schedule_time import next_fire
from ..persistence import watches as repo
from ..persistence.models import Watch
from ..persistence.schedules import KIND_ONCE
from .inprocess import (
    InProcessServerConfig,
    InProcessTool,
    create_sdk_mcp_server,
    tool,
)

_CREATE_DESCRIPTION = (
    "Set up a standing watch over one person's iMessage thread: an instruction "
    "chief follows the next time that person texts (e.g. 'if mom texts me today "
    "about dinner, tell her I'll be late'). Resolve the person to a handle with "
    "lookup_contact FIRST — watches only bind to known handles you've resolved, "
    "never bare names or unknown numbers. `target_handle` is that resolved "
    "handle (E.164 phone or email). `instruction` is what chief should do. "
    "Omit `expiry` for the default 14-day TTL, or give an ISO-8601 timestamp "
    "read in the owner's local time otherwise (e.g. wording like 'today' means "
    "local midnight tonight). `tone` is 'report' (default, tell the owner what "
    "happened) or 'silent' (act without narrating back)."
)
_LIST_DESCRIPTION = (
    "List every watch — its id, target, instruction, expiry, tone, and current "
    "state (armed/expired/cancelled). Use the id with cancel_watch."
)
_CANCEL_DESCRIPTION = (
    "Cancel a watch by its id (from list_watches) — it will never fire."
)

_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_handle": {
            "type": "string",
            "description": "The Contacts-resolved handle (E.164 phone or email).",
        },
        "instruction": {
            "type": "string",
            "description": "What chief should do when this person next texts.",
        },
        "expiry": {
            "type": "string",
            "description": (
                "ISO-8601 timestamp the watch expires at, read in the owner's "
                "local time. Omit for the 14-day default."
            ),
        },
        "tone": {
            "type": "string",
            "enum": list(repo.TONES),
            "description": "'report' (default) or 'silent'.",
        },
    },
    "required": ["target_handle", "instruction"],
}
_CANCEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"watch_id": {"type": "integer"}},
    "required": ["watch_id"],
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


def _fmt_local(dt: datetime, tz: tzinfo) -> str:
    """Format a stored (UTC) timestamp in the owner's timezone."""
    if dt.tzinfo is None:  # sqlite hands back naive UTC
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _describe(watch: Watch, tz: tzinfo, *, now: datetime) -> str:
    state = repo.effective_state(watch, now=now)
    return (
        f'#{watch.id} {watch.target_handle} — "{watch.instruction}" '
        f"({state}, expires {_fmt_local(watch.expiry, tz)}, {watch.tone} tone)"
    )


@dataclass(frozen=True)
class WatchService:
    """The owner's watch CRUD tools (create/list/cancel — no firing here)."""

    session_factory: async_sessionmaker[AsyncSession]
    owner_tz: str = "UTC"
    now: Callable[[], datetime] = _utcnow
    server_name: str = "chief_watches"

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The SDK-qualified tool names — added to the owner's allow-list."""
        n = self.server_name
        return (
            f"mcp__{n}__create_watch",
            f"mcp__{n}__list_watches",
            f"mcp__{n}__cancel_watch",
        )

    def _tz(self) -> tzinfo:
        return ZoneInfo(self.owner_tz)

    def _build_create(self) -> InProcessTool:
        factory, tz, now_fn = self.session_factory, self._tz(), self.now

        @tool("create_watch", _CREATE_DESCRIPTION, _CREATE_SCHEMA)
        async def create_watch(args: dict[str, Any]) -> dict[str, Any]:
            target_handle = _opt(args, "target_handle")
            instruction = _opt(args, "instruction")
            if not target_handle:
                return _text_result(
                    "Which handle? Resolve one with lookup_contact first.",
                    is_error=True,
                )
            if not instruction:
                return _text_result(
                    "What should chief do? Give an instruction.", is_error=True
                )
            tone = str(args.get("tone") or repo.TONE_REPORT)
            if tone not in repo.TONES:
                return _text_result(
                    f"Unknown tone {tone!r} (use 'report' or 'silent').",
                    is_error=True,
                )
            now = now_fn()
            expiry_arg = _opt(args, "expiry")
            expiry: datetime | None
            if expiry_arg is None:
                expiry = repo.default_expiry(now)
            else:
                try:
                    expiry = next_fire(KIND_ONCE, expiry_arg, after=now, tz=tz)
                except ValueError:
                    expiry = None
                if expiry is None:
                    return _text_result(
                        "Couldn't read that expiry (use ISO-8601, e.g. "
                        "2026-06-05T09:00).",
                        is_error=True,
                    )
                if expiry <= now:
                    return _text_result(
                        f"That expiry ({_fmt_local(expiry, tz)}) is already in "
                        "the past.",
                        is_error=True,
                    )
            async with factory() as session:
                watch = await repo.create_watch(
                    session,
                    target_handle=target_handle,
                    instruction=instruction,
                    expiry=expiry,
                    tone=tone,
                )
            return _text_result(
                f"Watching {watch.target_handle} — \"{watch.instruction}\" "
                f"until {_fmt_local(watch.expiry, tz)}, {watch.tone} tone."
            )

        return create_watch

    def _build_list(self) -> InProcessTool:
        factory, tz, now_fn = self.session_factory, self._tz(), self.now

        @tool("list_watches", _LIST_DESCRIPTION, {})
        async def list_watches(args: dict[str, Any]) -> dict[str, Any]:
            async with factory() as session:
                rows = await repo.list_watches(session)
            if not rows:
                return _text_result("No watches.")
            now = now_fn()
            return _text_result(
                "\n".join(_describe(w, tz, now=now) for w in rows)
            )

        return list_watches

    def _build_cancel(self) -> InProcessTool:
        factory, now_fn = self.session_factory, self.now

        @tool("cancel_watch", _CANCEL_DESCRIPTION, _CANCEL_SCHEMA)
        async def cancel_watch(args: dict[str, Any]) -> dict[str, Any]:
            watch_id = int(args["watch_id"])
            async with factory() as session:
                watch = await repo.get_watch(session, watch_id)
                if watch is None:
                    return _text_result(f"No watch #{watch_id}.", is_error=True)
                state = repo.effective_state(watch, now=now_fn())
                if state != repo.STATE_ARMED:
                    return _text_result(
                        f"#{watch_id} is already {state} — nothing to cancel."
                    )
                target_handle = watch.target_handle
                await repo.cancel_watch(session, watch_id)
            return _text_result(
                f"Cancelled #{watch_id} — {target_handle} will never fire."
            )

        return cancel_watch

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the watch tools."""
        return create_sdk_mcp_server(
            self.server_name,
            tools=[self._build_create(), self._build_list(), self._build_cancel()],
        )
