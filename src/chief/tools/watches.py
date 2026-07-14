"""chief's watch tools (#165/#168, part of PRD #160) — standing owner instructions.

One in-process server (``chief_watches``), owner-only, wired into owner sessions
whenever the iMessage adapter is configured. Five CRUD tools, plus a sixth
(``reply_to_watch``) added only for the iMessage owner session (#167):

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
  ``target_handle`` may be omitted (#168) — the person isn't a known handle
  yet, so the watch starts unbound and the owner confirms it later via
  ``confirm_watch_candidate``.
- ``list_watches`` — every watch with its effective state (an armed watch past
  its expiry reads as ``expired``, per
  :func:`chief.persistence.watches.effective_state`); an unbound watch's target
  reads as ``(unbound — awaiting confirmation)``.
- ``cancel_watch`` — flip an armed watch to ``cancelled`` for good; a cancelled
  or already-expired watch can never fire, and cancelling twice is a no-op.
- ``list_watch_candidates`` (#168) — pending unknown-sender candidates the
  iMessage adapter surfaced (see :mod:`chief.adapters.imessage`), each tied to
  the unbound watch it might match.
- ``confirm_watch_candidate`` (#168) — resolve one: "yes" binds the watch's
  ``target_handle`` to that candidate's handle (making it an ordinary armed
  watch, indistinguishable from a directly-created one); "no" rejects it and
  the sender stays inert.

- ``reply_to_watch`` (#167) — fire a watch: send a reply to its stored contact AS
  THE OWNER through the guarded iMessage send seam and retire the watch (single-fire;
  ``keep_watching=true`` keeps a standing instruction armed). The model can only
  target a watch's own ``target_handle``, never a free-form handle. Because this tool
  runs in the owner session — the same one that hosts the untrusted watched-message
  eval turn — a prompt-injected turn could otherwise mint its own watch and fire it;
  the :class:`WatchFireGate` closes that by only letting a watch a real inbound
  dispatched an eval for fire (the seam's watch check is then a redundant backstop,
  not the sole control). A refused/undeliverable send RAISES
  (:class:`GhostSendRefused`), so the watch stays armed and no false confirmation is
  posted. A ``report``-tone fire also posts a confirmation to the self-thread; a
  ``silent`` one does not. Added only when the session carries the send seam +
  front-desk route (the iMessage owner session).

The CRUD five are owner-initiated and reversible-enough (cancel always undoes a
watch; a wrong-target create is caught by the confirmation echo) → pre-approved, no
card, the same posture as
:class:`~chief.tools.imessage_admin.IMessageAdminService`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any, Protocol
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
    "handle (E.164 phone or email). Omit `target_handle` only when the person is "
    "NOT a known contact/handle yet (e.g. an expected text from an unknown "
    "number) — chief will surface each new sender as a candidate in the "
    "self-thread for you to confirm before the watch binds. `instruction` is "
    "what chief should do. Omit `expiry` for the default 14-day TTL, or give an "
    "ISO-8601 timestamp read in the owner's local time otherwise (e.g. wording "
    "like 'today' means local midnight tonight). `tone` is 'report' (default, "
    "tell the owner what happened) or 'silent' (act without narrating back)."
)
_LIST_DESCRIPTION = (
    "List every watch — its id, target, instruction, expiry, tone, and current "
    "state (armed/expired/cancelled). Use the id with cancel_watch."
)
_CANCEL_DESCRIPTION = (
    "Cancel a watch by its id (from list_watches) — it will never fire."
)
_LIST_CANDIDATES_DESCRIPTION = (
    "List pending unknown-sender candidates — handle + when first seen, and "
    "which unbound watch they might match. Use the id with confirm_watch_candidate."
)
_CONFIRM_CANDIDATE_DESCRIPTION = (
    "Confirm or reject a pending unknown-sender candidate (from "
    "list_watch_candidates). 'yes' binds the watch to that handle; 'no' rejects "
    "it and the sender stays inert."
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
    "required": ["instruction"],
}
_CANCEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"watch_id": {"type": "integer"}},
    "required": ["watch_id"],
}
_CONFIRM_CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidate_id": {"type": "integer"},
        "decision": {"type": "string", "enum": ["yes", "no"]},
    },
    "required": ["candidate_id", "decision"],
}

_REPLY_DESCRIPTION = (
    "Fire a watch: reply to the watched contact AS THE OWNER and close the watch "
    "(#167). Use this ONLY from a watch evaluation turn, when the watched person's "
    "message is relevant to the standing instruction. `watch_id` is that watch; "
    "`text` is the reply — it is sent to the watch's own stored contact and can "
    "NEVER target any other handle. By default the watch is single-fire and "
    "retires after this reply; pass `keep_watching=true` only for a standing/"
    "ongoing instruction that should keep firing on future messages. A 'report'-"
    "tone watch also posts a confirmation to your self-thread; a 'silent'-tone "
    "watch sends only to the contact and stays quiet."
)
_REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "watch_id": {"type": "integer"},
        "text": {
            "type": "string",
            "description": "The reply sent to the watched contact as the owner.",
        },
        "keep_watching": {
            "type": "boolean",
            "description": (
                "Keep the watch armed after this reply (ongoing instruction). "
                "Omit for the default single-fire retire."
            ),
        },
    },
    "required": ["watch_id", "text"],
}


class WatchSend(Protocol):
    """The send slice of the iMessage IO the fire tool drives (#167)."""

    async def send(self, thread_key: str, text: str) -> None: ...


class GhostSendRefused(RuntimeError):
    """The send seam refused a self-DM ghost-send (#167).

    Raised by :class:`chief.adapters.imessage.IMessageTaskIO` when a send to a
    non-self handle isn't authorized by any active watch. It is surfaced (not
    swallowed) so :func:`reply_to_watch` reports the delivery failure and leaves the
    watch armed, instead of retiring it and posting a false "✅ Replied" — the #167
    medium finding: a parked/denied fire must never read as a success.
    """


class WatchFireGate:
    """Which watches an actual inbound eval has cleared to fire (#167 hardening).

    The watched-message evaluation turn runs in the owner session, where both
    ``create_watch`` and ``reply_to_watch`` are pre-approved. Without this gate a
    prompt-injected turn could mint its own armed watch on an attacker handle and
    fire it, so the send seam's watch check would authorize chief's own freshly
    minted watch and exfiltrate owner-authored text to an arbitrary handle.

    The adapter :meth:`~chief.adapters.imessage.IMessageAdapter._admit_watched`
    :meth:`authorize`\\ s a fire ONLY for the watch(es) a real incoming message
    dispatched an eval for — those already passed the created-before-arrival
    admission gate. :func:`reply_to_watch` refuses any other ``watch_id``. A watch
    minted inside the eval turn was never dispatched, so it can never fire. The
    record is in-process and fails closed across a restart (a standing watch simply
    re-authorizes on its next inbound).
    """

    def __init__(self) -> None:
        self._authorized: set[int] = set()

    def authorize(self, watch_id: int) -> None:
        self._authorized.add(watch_id)

    def is_authorized(self, watch_id: int) -> bool:
        return watch_id in self._authorized

    def consume(self, watch_id: int) -> None:
        """Drop a single-fire watch's clearance once it has fired and retired."""
        self._authorized.discard(watch_id)


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
    target = watch.target_handle or "(unbound — awaiting confirmation)"
    return (
        f'#{watch.id} {target} — "{watch.instruction}" '
        f"({state}, expires {_fmt_local(watch.expiry, tz)}, {watch.tone} tone)"
    )


@dataclass(frozen=True)
class WatchService:
    """The owner's watch CRUD tools (create/list/cancel — no firing here)."""

    session_factory: async_sessionmaker[AsyncSession]
    owner_tz: str = "UTC"
    now: Callable[[], datetime] = _utcnow
    server_name: str = "chief_watches"
    #: The guarded send seam + self-thread route the fire tool needs (#167). Both are
    #: set only for the iMessage owner session; without them, no ``reply_to_watch``.
    send: WatchSend | None = None
    front_desk: str | None = None
    #: The eval-turn fire gate (#167): only watches a real inbound dispatched an eval
    #: for may fire, so a hijacked turn can't mint and fire its own watch. ``None`` in
    #: unit tests that drive ``reply_to_watch`` directly; production always wires it.
    fire_gate: WatchFireGate | None = None

    @property
    def _can_fire(self) -> bool:
        return self.send is not None and self.front_desk is not None

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The SDK-qualified tool names — added to the owner's allow-list."""
        n = self.server_name
        names: tuple[str, ...] = (
            f"mcp__{n}__create_watch",
            f"mcp__{n}__list_watches",
            f"mcp__{n}__cancel_watch",
            f"mcp__{n}__list_watch_candidates",
            f"mcp__{n}__confirm_watch_candidate",
        )
        if self._can_fire:
            names = (*names, f"mcp__{n}__reply_to_watch")
        return names

    def _tz(self) -> tzinfo:
        return ZoneInfo(self.owner_tz)

    def _build_create(self) -> InProcessTool:
        factory, tz, now_fn = self.session_factory, self._tz(), self.now

        @tool("create_watch", _CREATE_DESCRIPTION, _CREATE_SCHEMA)
        async def create_watch(args: dict[str, Any]) -> dict[str, Any]:
            target_handle = _opt(args, "target_handle")
            instruction = _opt(args, "instruction")
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
            if watch.target_handle:
                msg = (
                    f"Watching {watch.target_handle} — \"{watch.instruction}\" "
                    f"until {_fmt_local(watch.expiry, tz)}, {watch.tone} tone."
                )
            else:
                msg = (
                    f'Watching for an unknown sender — "{watch.instruction}" '
                    f"until {_fmt_local(watch.expiry, tz)}, {watch.tone} tone. "
                    "I'll ask you to confirm the handle in this thread when "
                    "someone new texts."
                )
            return _text_result(msg)

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
                target_handle = watch.target_handle or "it"
                await repo.cancel_watch(session, watch_id)
            return _text_result(
                f"Cancelled #{watch_id} — {target_handle} will never fire."
            )

        return cancel_watch

    def _build_list_candidates(self) -> InProcessTool:
        factory, tz, now_fn = self.session_factory, self._tz(), self.now

        @tool("list_watch_candidates", _LIST_CANDIDATES_DESCRIPTION, {})
        async def list_watch_candidates(args: dict[str, Any]) -> dict[str, Any]:
            async with factory() as session:
                candidates = await repo.list_pending_candidates(
                    session, now=now_fn()
                )
            if not candidates:
                return _text_result("No pending candidates.")
            lines = [
                f"#{c.id} {c.handle} — for watch #{c.watch_id}, first seen "
                f"{_fmt_local(c.first_seen, tz)}"
                for c in candidates
            ]
            return _text_result("\n".join(lines))

        return list_watch_candidates

    def _build_confirm_candidate(self) -> InProcessTool:
        factory, now_fn = self.session_factory, self.now

        @tool(
            "confirm_watch_candidate",
            _CONFIRM_CANDIDATE_DESCRIPTION,
            _CONFIRM_CANDIDATE_SCHEMA,
        )
        async def confirm_watch_candidate(args: dict[str, Any]) -> dict[str, Any]:
            candidate_id = int(args["candidate_id"])
            decision = str(args.get("decision") or "")
            if decision not in ("yes", "no"):
                return _text_result(
                    "decision must be 'yes' or 'no'.", is_error=True
                )
            async with factory() as session:
                result = await repo.confirm_candidate(
                    session,
                    candidate_id,
                    confirm=(decision == "yes"),
                    now=now_fn(),
                )
            if result is None:
                return _text_result(
                    f"No pending candidate #{candidate_id} (missing, already "
                    "decided, or its watch expired).",
                    is_error=True,
                )
            watch, candidate = result
            if decision == "yes":
                return _text_result(
                    f"Confirmed — watch #{watch.id} now watches {candidate.handle}."
                )
            return _text_result(f"Ignored — {candidate.handle} stays inert.")

        return confirm_watch_candidate

    def _build_reply(self) -> InProcessTool:
        factory, now_fn = self.session_factory, self.now
        send, front_desk = self.send, self.front_desk
        fire_gate = self.fire_gate
        assert send is not None and front_desk is not None  # gated by _can_fire

        @tool("reply_to_watch", _REPLY_DESCRIPTION, _REPLY_SCHEMA)
        async def reply_to_watch(args: dict[str, Any]) -> dict[str, Any]:
            watch_id = int(args["watch_id"])
            text = _opt(args, "text")
            if not text:
                return _text_result("What should I say? Give a reply.", is_error=True)
            keep_watching = bool(args.get("keep_watching"))
            async with factory() as session:
                watch = await repo.get_watch(session, watch_id)
                if watch is None:
                    return _text_result(f"No watch #{watch_id}.", is_error=True)
                if (
                    repo.effective_state(watch, now=now_fn()) != repo.STATE_ARMED
                    or not watch.target_handle
                ):
                    return _text_result(
                        f"Watch #{watch_id} is not active — fired, expired, "
                        "cancelled, or unbound.",
                        is_error=True,
                    )
                handle, tone = watch.target_handle, watch.tone
            # Age-gate (#167): only a watch a real incoming message dispatched an eval
            # for may fire, so a hijacked eval turn can't mint its own watch and fire
            # it. A watch minted inside this turn was never authorized by the adapter.
            if fire_gate is not None and not fire_gate.is_authorized(watch_id):
                return _text_result(
                    f"Watch #{watch_id} can't be fired here — only a watch a real "
                    "incoming message triggered may reply.",
                    is_error=True,
                )
            # The active watch authorizes this at the send seam, which sends argv +
            # records the own-send so its echo is consumed, never re-evaluated. A
            # refusal there RAISES (never a silent drop) so we don't retire the watch
            # or post a false "✅ Replied".
            try:
                await send.send(handle, text)
            except GhostSendRefused:
                return _text_result(
                    f"Couldn't reach {handle} — the send was refused; watch "
                    f"#{watch_id} stays armed.",
                    is_error=True,
                )
            if not keep_watching:
                async with factory() as session:
                    await repo.retire_watch(session, watch_id)
                if fire_gate is not None:
                    fire_gate.consume(watch_id)
            if tone == repo.TONE_REPORT:
                await send.send(
                    front_desk,
                    f'✅ Replied to {handle} for watch #{watch_id}: "{text}"',
                )
            status = "still armed" if keep_watching else "retired"
            return _text_result(f"Replied to {handle}; watch #{watch_id} {status}.")

        return reply_to_watch

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the watch tools."""
        tools = [
            self._build_create(),
            self._build_list(),
            self._build_cancel(),
            self._build_list_candidates(),
            self._build_confirm_candidate(),
        ]
        if self._can_fire:
            tools.append(self._build_reply())
        return create_sdk_mcp_server(self.server_name, tools=tools)
