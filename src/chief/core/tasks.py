"""Task engine — the core loop (DESIGN: task execution engine).

A conversation *is* a task: one persistent :class:`TaskSession` per ``thread_key``, kept
in a registry. Each task owns an input queue drained by a single consumer, so its turns
run in order. Concurrency across tasks is bounded by a semaphore (only *generating*
turns hold a slot — idle sessions cost nothing). Behaviours:

- **Hybrid inline/background.** A turn taking longer than ``grace_seconds`` posts a
  "working…" ack; fast turns just reply, feeling synchronous. All output goes through
  :class:`TaskIO`, so "inline" and "background" share a code path — only timing differs.
- **Steering (auto-detect with Haiku).** A message arriving mid-turn is queued as the
  next turn; if ``stop_intent`` flags it as a stop/redirect it also ``interrupt()``s the
  running turn. ``/cancel`` interrupts deterministically.
- **Auto-spawn topics.** A General-topic message that ``warrants_task`` becomes a new
  tracked topic via :meth:`TaskIO.create_thread`.
- **Idle archive.** After ``idle_archive_seconds`` of inactivity a task is marked done
  and its thread archived; the next message reopens it (resume).
- **Casual self-compaction.** A casual ``:0`` channel can't archive (it has no closable
  topic) and would otherwise resume an ever-growing transcript every turn. So after
  ``compaction_idle_seconds`` of inactivity it summarizes its own conversation and
  reseeds onto a fresh session resumed from that brief — replicating ``/compact``, which
  the SDK exposes no programmatic trigger for — and stays OPEN instead of dying.
- **Restart recovery.** :meth:`recover` pings the owner for tasks left mid-flight and
  never auto-resumes; a follow-up message resumes from the persisted ``sdk_session_id``.

The engine is platform-neutral: speaks only :class:`TaskIO` (provided by the adapter).
"""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from claude_agent_sdk import CanUseTool, HookMatcher
from claude_agent_sdk.types import HookEvent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.base import (
    FILE_REPLY_NOTE,
    Attachment,
    Surface,
    reply_filename,
    should_send_as_file,
)
from ..gate.approvals import OPUS_ESCALATION_KIND, ApprovalManager
from ..gate.gate import (
    BUILTIN_SHELL_TOOLS,
    FILE_OP_TOOLS,
    WRITE_OP_TOOLS,
    build_can_use_tool,
    build_pretool_hook,
)
from ..gate.policy import PolicyStore
from ..memory.store import MemoryStore
from ..memory.versioning import NullVersioner, Versioner
from ..obs.audit import AuditLog
from ..persistence import usage
from ..persistence.models import Task
from ..persistence.tasks import (
    CANCELLED,
    DONE,
    FAILED,
    OPEN,
    RUNNING,
    TERMINAL,
    WAITING,
    get_or_create_task,
    get_task,
    list_active,
    set_active_account,
    set_session_id,
    set_status,
    set_task_model,
)
from ..tools.browser.screenshot import build_screenshot_hook
from ..tools.calendar import mcp as calendar_mcp
from ..tools.drive import mcp as drive_mcp
from ..tools.gmail import mcp as gmail_mcp
from ..tools.google import GoogleService
from ..tools.google.account_selection import (
    ACCOUNT_SELECTION_GUIDANCE,
    extract_account_hint,
)
from ..tools.google.add_account_service import AddAccountService
from ..tools.google.list_accounts_service import ListAccountsService
from ..tools.google.set_account_service import SetAccountService
from ..tools.guest import GuestAdminService, GuestService
from ..tools.schedule import ScheduleBashService, ScheduleService
from ..tools.sheets import mcp as sheets_mcp
from ..tools.shell import ShellService
from . import classify
from .agent import NO_REPLY
from .personas import build_system_prompt
from .session import Final, TaskSession, TurnEvent

logger = logging.getLogger("chief.core.tasks")

WORKING_ACK = "working on it…"
#: User-visible note when the per-turn watchdog fires: a turn ran past
#: ``turn_timeout`` without a terminal result (a wedged SDK stream / hung tool call),
#: so the engine tears the session down rather than freezing the task forever.
TURN_TIMEOUT_NOTE = "⚠️ that turn timed out — try again."
#: Bound on the watchdog's own teardown of a wedged session: ``interrupt`` may hang on
#: the same wedged control stream, so it is time-boxed before ``aclose`` forces a fresh
#: subprocess on the next turn. Keeps a hung teardown from re-freezing the consumer.
_RESET_TIMEOUT = 10.0
#: Owner reminder when a turn is skipped because the cycle is paused at budget (M9).
#: The choice card was already posted when the cycle paused; this nudges once per
#: pause episode so queued turns don't silently vanish while the owner hasn't decided.
PAUSED_BUDGET_ACK = "⏸ Paused at budget — pick how to continue on the card."
#: Owner-facing confirmations for the M11 Opus escalation. ``/opus`` (or an approved
#: auto-detect card) switches the thread to Opus and persists it (Task.model) until
#: ``/sonnet`` reverts. OPUS_BUDGET_NOTE is appended when the cycle is downgraded, since
#: escalating overrides the downgrade and so burns the monthly credit faster.
OPUS_CONFIRM = "⚡ Switched to Opus 4.8 for this thread — /sonnet to switch back."
OPUS_BUDGET_NOTE = "Heads up: Opus burns the monthly budget faster."
SONNET_CONFIRM = "↩️ Back to Sonnet 4.6 for this thread."
#: Read-only file tools chief gets at M4, confined to the memory dir by the gate.
MEMORY_TOOLS = sorted(FILE_OP_TOOLS)
#: Write file tools the owner gets at M7 when the workspace is enabled — added to
#: ``allowed_tools`` but confined to memory ∪ workspace by the gate (writes elsewhere
#: DENY).
WORKSPACE_TOOLS = sorted(WRITE_OP_TOOLS)
#: Built-in shell tools refused outright at the SDK layer (belt-and-braces with the
#: gate's hard DENY) — they run inside core where the Max token lives, so the model can
#: never reach them; it uses the sandbox shell (``mcp__chief_shell__bash``) instead.
DISALLOWED_BUILTINS = sorted(BUILTIN_SHELL_TOOLS)
#: Read-only web + meta tools the owner agent always gets (the gate treats all three as
#: read-only/safe — see gate.READ_ONLY). ToolSearch loads deferred MCP tool schemas.
WEB_META_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch", "ToolSearch")
#: The owner-only built-ins a guest must never reach. The gate classifies these as
#: read-only (ALLOW), so absence from a guest's allowed_tools is not enough — they are
#: refused at the SDK layer (disallowed_tools), the same hard-deny used for the shell.
GUEST_DENIED = sorted(set(MEMORY_TOOLS) | set(WORKSPACE_TOOLS) | set(WEB_META_TOOLS))
#: System-prompt note appended to an owner session running on a GROUP surface (M11).
#: Same owner toolset, but a reminder that replies are public to the whole group and
#: that tool-approval prompts are DM'd privately — so the model neither leaks
#: owner-private context into the room nor waits on a card it can't see there.
GROUP_MODE_NOTE = (
    "GROUP CHAT: You are replying in a shared group, not a private DM — everyone in "
    "the group sees what you post. Do not disclose the owner's private, personal, or "
    "confidential information, secrets, or anything from private DMs or memory that "
    "the group shouldn't see. When a tool you call needs approval, that prompt is sent "
    "privately to the owner's DM, never shown here — don't announce it or wait for it "
    "in the group; just continue once it resolves."
)
#: One-shot prompt that asks the casual session to brief its own history (the live
#: session, not the engine's pruned transcript, so the brief sees the full context).
COMPACT_PROMPT = (
    "Summarize our conversation so far into a compact brief for your future self: the "
    "durable context, any open threads, and the gist of recurring topics. Reply with "
    "only the brief, nothing else."
)
#: Frames the brief as carried-forward context when priming the reseeded casual session,
#: so the summary lands in the new transcript (and survives a restart via the new id).
PRIME_TEMPLATE = (
    "Here is a brief of our earlier conversation, to carry forward as context:\n\n"
    "{summary}"
)


@dataclass(frozen=True)
class Turn:
    """One queued unit of owner work: the message text plus any inbound media (M8)."""

    text: str
    attachments: tuple[Attachment, ...] = ()


class TaskIO(Protocol):
    """How the engine talks back to a platform (implemented by the adapter)."""

    async def send(self, thread_key: str, text: str) -> None: ...
    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None: ...
    async def create_thread(self, *, like_thread_key: str, title: str) -> str: ...
    async def archive_thread(self, thread_key: str) -> None: ...


class SessionProto(Protocol):
    """The slice of :class:`TaskSession` the engine drives (structural)."""

    session_id: str | None
    #: This turn's SDK cost and latest rate-limit status, captured by the session and
    #: read by the engine after a clean turn to drive the budget (M9).
    last_cost_usd: float
    last_rate_limit_status: str | None

    def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]: ...
    async def interrupt(self) -> None: ...
    async def set_model(self, model: str) -> None: ...
    async def aclose(self) -> None: ...


class BudgetProto(Protocol):
    """The slice of :class:`~chief.core.budget.BudgetGate` the engine drives (M9)."""

    async def record(self, cost: float) -> None: ...
    async def note_rate_limited(self) -> None: ...
    async def mode(self) -> str: ...


SessionFactory = Callable[..., SessionProto]
Classifier = Callable[..., Awaitable[bool]]


def _default_session(
    *,
    model: str,
    resume: str | None = None,
    fork_session: bool = False,
    can_use_tool: CanUseTool | None = None,
    hooks: dict[HookEvent, list[HookMatcher]] | None = None,
    system_prompt: str | None = None,
    cwd: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    mcp_servers: dict[str, Any] | None = None,
    plugins: list[Any] | None = None,
    skills: list[str] | None = None,
) -> SessionProto:
    return TaskSession(
        model=model,
        resume=resume,
        fork_session=fork_session,
        can_use_tool=can_use_tool,
        hooks=hooks,
        system_prompt=system_prompt,
        cwd=cwd,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        mcp_servers=mcp_servers,
        plugins=plugins,
        skills=skills,
    )


def _title(text: str) -> str:
    first = next((line for line in text.strip().splitlines() if line.strip()), "task")
    return first.strip()[:60] or "task"


@dataclass
class _RunningTask:
    thread_key: str
    db_id: int
    session: SessionProto
    queue: "asyncio.Queue[Turn]"
    tier: str
    is_casual: bool = False
    generating: bool = False
    cancelled: bool = False
    consumer: "asyncio.Task[None] | None" = None
    idle_handle: "asyncio.Task[None] | None" = None
    transcript: list[tuple[str, str]] = field(default_factory=list)
    surface: Surface = Surface.DM
    #: The model this live session is running on (M11). Mirrors the SDK session's model
    #: so the "already on Opus" guard and the auto-escalate skip are O(1) (no DB read).
    model: str = ""
    #: Set when an auto-detect escalation card was denied, so a later complex turn in
    #: the same task doesn't re-ask. Cleared by an explicit /opus or /sonnet.
    auto_escalate_suppressed: bool = False


class TaskManager:
    """Owns the live task sessions and the rules that drive them."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        io: TaskIO,
        owner_model: str,
        classifier_model: str,
        platform: str = "telegram",
        concurrency: int = 3,
        grace_seconds: float = 6.0,
        turn_timeout: float = 300.0,
        idle_archive_seconds: float = 3600.0,
        compaction_idle_seconds: float = 3600.0,
        message_limit: int = 4096,
        session_factory_sdk: SessionFactory = _default_session,
        stop_intent: Classifier = classify.stop_intent,
        warrants_task: Classifier = classify.warrants_task,
        is_complex: Classifier = classify.is_complex,
        owner_model_opus: str = "claude-opus-4-8",
        opus_auto_detect: bool = False,
        policy: PolicyStore | None = None,
        approvals: ApprovalManager | None = None,
        audit: AuditLog | None = None,
        front_desk_thread_key: str | None = None,
        memory: MemoryStore | None = None,
        memory_dir: str | None = None,
        owner_name: str = "the owner",
        google_services: Sequence[GoogleService] = (),
        owner_tz: str = "UTC",
        shell_service: ShellService | None = None,
        workspace_dir: str | None = None,
        guest_model: str | None = None,
        guest_calendar_service: GoogleService | None = None,
        guest_admin_service: GuestAdminService | None = None,
        list_accounts_service: ListAccountsService | None = None,
        set_account_service: SetAccountService | None = None,
        add_account_service: AddAccountService | None = None,
        schedule_service: ScheduleService | None = None,
        schedule_bash_service: ScheduleBashService | None = None,
        budget: BudgetProto | None = None,
        owner_inbox: str | None = None,
        budget_downgrade_model: str | None = None,
        skills_enabled: bool = False,
        skills_plugin_path: str | None = None,
        default_skills: tuple[str, ...] = (),
        group_context_max_messages: int = 50,
        versioner: Versioner | None = None,
        screenshots_dir: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._io = io
        self._owner_model = owner_model
        self._classifier_model = classifier_model
        self._platform = platform
        self._grace_seconds = grace_seconds
        self._turn_timeout = turn_timeout
        self._idle_archive_seconds = idle_archive_seconds
        self._compaction_idle_seconds = compaction_idle_seconds
        self._message_limit = message_limit
        self._session_factory_sdk = session_factory_sdk
        self._stop_intent = stop_intent
        self._warrants_task = warrants_task
        # Owner model posture (M11): default owner_model (Sonnet); escalate to
        # owner_model_opus via /opus or — when opus_auto_detect is on — an approved
        # per-turn complexity check. is_complex is injected for testability.
        self._is_complex = is_complex
        self._owner_model_opus = owner_model_opus
        self._opus_auto_detect = opus_auto_detect
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
        self._front_desk_thread_key = front_desk_thread_key
        self._memory = memory
        self._memory_dir = memory_dir
        self._owner_name = owner_name
        self._google_services = tuple(google_services)
        self._owner_tz = owner_tz
        self._shell_service = shell_service
        self._workspace_dir = workspace_dir
        self._guest_model = guest_model
        self._guest_calendar_service = guest_calendar_service
        self._guest_admin_service = guest_admin_service
        self._list_accounts_service = list_accounts_service
        self._set_account_service = set_account_service
        self._add_account_service = add_account_service
        self._schedule_service = schedule_service
        self._schedule_bash_service = schedule_bash_service
        self._budget = budget
        self._owner_inbox = owner_inbox
        self._budget_downgrade_model = budget_downgrade_model
        # Skills (M10), owner-only. When enabled, owner sessions load the plugin
        # manifest at skills_plugin_path and enable exactly default_skills; guests get
        # neither.
        self._skills_enabled = skills_enabled
        self._skills_plugin_path = skills_plugin_path
        self._default_skills = default_skills
        # Screenshot delivery (issue #34): when set, the PostToolUse hook reads
        # screenshots from this dir (shared volume) and delivers them via send_file.
        self._screenshots_dir = screenshots_dir
        #: Set once the owner is reminded a paused cycle is blocking turns; cleared the
        #: next time the budget reports a non-paused mode, so each pause acks once.
        self._paused_ack_sent = False
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: dict[str, _RunningTask] = {}
        # Per-group ambient buffer (M11): non-engaged group messages, attributed and
        # bounded, drained into the next engaged turn so a reply sees the whole thread.
        self._group_context_max = group_context_max_messages
        self._group_buffers: dict[str, deque[tuple[str, str]]] = {}
        # Versioner for memory auto-commit (#22): commit after every turn so the
        # memory dir's git history tracks each session write. The versioner
        # self-serializes concurrent callers via its internal asyncio.Lock (#29).
        # NullVersioner when unset — no commit overhead for tests / no-git runs.
        self._versioner: Versioner = (
            versioner if versioner is not None else NullVersioner()
        )

    # ---- scheduler hooks -------------------------------------------------

    @property
    def io(self) -> TaskIO:
        """The platform IO this manager drives (the scheduler delivers through it)."""
        return self._io

    @property
    def platform(self) -> str:
        """This manager's platform — the scheduler is built against the primary one."""
        return self._platform

    async def wake(self, *, thread_key: str, text: str) -> None:
        """Boot an agent turn from a scheduled wakeup (M9) — like an owner message.

        Reuses the whole machinery (gate, tool surface, approval cards, idle timers): a
        woken turn re-passes the permission gate, so any effectful tool
        it reaches still raises an approval card — unattended, that card fail-closed
        denies. That is what lets the scheduler treat ``wakeup`` creation as benign.
        """
        # is_casual=False (the _ensure_task default): a wakeup targets a real topic —
        # the primary inbox or a tracked thread, not the lossy casual General channel
        # (whose casual flag needs the adapter's is_forum, unavailable here). A cold
        # wake onto General (":0") just runs as a normal task; its idle-archive no-ops
        # there (archive_thread guards thread_id==0), so nothing is wrongly closed.
        task = await self._ensure_task(thread_key)
        await self._submit(task, Turn(text=text))

    # ---- inbound routing -------------------------------------------------

    async def observe(
        self, *, thread_key: str, text: str, sender_name: str | None = None
    ) -> None:
        """Buffer a non-engaged group message as ambient context (M11), no reply.

        chief reads every message in a group it's in (sender-attributed) but stays
        silent until engaged; this appends to the group's bounded buffer so the next
        engaged turn answers with the whole conversation in view.
        """
        buf = self._group_buffers.get(thread_key)
        if buf is None:
            buf = deque(maxlen=self._group_context_max)
            self._group_buffers[thread_key] = buf
        buf.append((sender_name or "someone", text))

    def _with_group_context(self, group_key: str, text: str) -> str:
        """Prepend the group's buffered ambient messages to an engaged turn, then drop.

        Draining keeps a later turn from re-reading lines the session already saw; the
        buffer is bounded (``group_context_max_messages``) so it can't grow without end.
        Popping the (now-empty) deque keeps ``_group_buffers`` from accreting one entry
        per group ever seen. The attributed lines are *untrusted* — both name and text
        come from arbitrary group members — so the GROUP_MODE_NOTE warns the model not
        to treat them as instructions or to disclose private context in reply.
        """
        buf = self._group_buffers.pop(group_key, None)
        if not buf:
            return text
        lines = "\n".join(f"{name}: {msg}" for name, msg in buf)
        return f"Recent group messages:\n{lines}\n\n{text}"

    async def dispatch(
        self,
        *,
        thread_key: str,
        text: str,
        attachments: tuple[Attachment, ...] = (),
        is_general: bool = False,
        surface: Surface = Surface.DM,
    ) -> None:
        """Route an owner message (+ media) into its task, spawning a topic when due."""
        if surface is Surface.GROUP:
            # An owner-engaged group turn runs flat (a group has no forum to branch
            # into) with the full owner surface; its approval cards DM the owner.
            text = self._with_group_context(thread_key, text)
            task = await self._ensure_task(thread_key, surface=Surface.GROUP)
            await self._submit(task, Turn(text=text, attachments=attachments))
            return
        is_casual = is_general
        if is_general and await self._warrants_task(
            text, model=self._classifier_model
        ):
            new_key = await self._io.create_thread(
                like_thread_key=thread_key, title=_title(text)
            )
            await self._io.send(thread_key, "→ Tracking that in a new topic.")
            thread_key = new_key
            is_casual = False  # a spawned topic is a real, full-memory task
        task = await self._ensure_task(thread_key, is_casual=is_casual)
        await self._submit(task, Turn(text=text, attachments=attachments))

    async def dispatch_guest(
        self,
        *,
        thread_key: str,
        text: str,
        from_label: str | None = None,
        surface: Surface = Surface.DM,
    ) -> None:
        """Route a guest message into a flat receptionist session (no topic spawn).

        Unlike :meth:`dispatch`, a guest never spawns a forum topic: a 1:1 DM is one
        flat session keyed by its ``thread_key``. ``from_label`` is the sender's display
        name, baked into the relay tool so the owner sees who left a message. On a GROUP
        surface the receptionist gets its own ``:guest`` session key so a non-owner can
        never reuse the owner's group session, but reads the same shared ambient buffer.
        """
        session_key = thread_key
        if surface is Surface.GROUP:
            text = self._with_group_context(thread_key, text)
            session_key = f"{thread_key}:guest"
        task = await self._ensure_task(
            session_key, tier="guest", from_label=from_label, surface=surface
        )
        # Guests stay text-only — media intake is owner-only (tier isolation, M8).
        await self._submit(task, Turn(text=text))

    async def cancel(self, thread_key: str) -> bool:
        """Stop the task in ``thread_key``; False if there was nothing to stop."""
        task = self._tasks.get(thread_key)
        if task is None:
            async with self._session_factory() as session:
                db = await get_task(
                    session, platform=self._platform, thread_key=thread_key
                )
                if db is None or db.status in TERMINAL:
                    return False
                await set_status(session, db, CANCELLED)
            return True
        task.cancelled = True
        if task.generating:
            await task.session.interrupt()
        await self._stop_task(task)
        await self._set_status(task, CANCELLED)
        return True

    async def active_tasks(self) -> list[Task]:
        """Non-terminal tasks, for the ``/tasks`` listing."""
        async with self._session_factory() as session:
            return await list_active(session, platform=self._platform)

    async def recover(self) -> None:
        """Ping the owner about tasks left mid-flight by a restart (no auto-resume)."""
        async with self._session_factory() as session:
            active = await list_active(session, platform=self._platform)
            pings: list[tuple[str, str | None]] = []
            for db in active:
                if db.status in (RUNNING, WAITING):
                    pings.append((db.thread_key, db.title))
                    await set_status(session, db, OPEN)
        for thread_key, title in pings:
            label = title or thread_key
            await self._io.send(
                thread_key,
                f"⚠️ Task “{label}” was interrupted by a restart. "
                "Send a message to resume it, or /cancel to drop it.",
            )

    async def shutdown(self) -> None:
        """Cancel timers/consumers and close sessions (clean teardown)."""
        for task in list(self._tasks.values()):
            await self._stop_task(task)

    # ---- per-task machinery ---------------------------------------------

    async def _ensure_task(
        self,
        thread_key: str,
        *,
        tier: str = "owner",
        is_casual: bool = False,
        from_label: str | None = None,
        surface: Surface = Surface.DM,
    ) -> _RunningTask:
        existing = self._tasks.get(thread_key)
        if existing is not None:
            if not existing.cancelled:
                return existing
            # An idle teardown (archive or casual reseed) is in flight; wait it out so
            # the DB settles (DONE + closed, or the reseeded id) before we reopen.
            if existing.idle_handle is not None:
                try:
                    await existing.idle_handle
                except asyncio.CancelledError:
                    pass
        async with self._session_factory() as session:
            db = await get_or_create_task(
                session, platform=self._platform, thread_key=thread_key, tier=tier
            )
            db_id, resume, persisted_model = db.id, db.sdk_session_id, db.model
        # Read the thread's active account (if any) for credential injection
        # into the calendar MCP server config (issue #46). Owner-only; guests
        # never have an active account binding.
        active_account_label: str | None = None
        account_ask_needed = False
        if tier == "owner" and self._set_account_service is not None:
            active_account_label = (
                await self._set_account_service.get_active_account_label(thread_key)
            )
            # Issue #49: selection precedence — if no explicit binding, try memory.
            if active_account_label is None:
                accounts = self._set_account_service.accounts
                n_accounts = len(accounts)
                if n_accounts > 0 and self._memory is not None:
                    hint = extract_account_hint(self._memory, accounts)
                    if hint is not None:
                        # Auto-select from memory and persist so next call skips this.
                        async with self._session_factory() as session:
                            db_task = await get_or_create_task(
                                session,
                                platform=self._platform,
                                thread_key=thread_key,
                                tier="owner",
                            )
                            await set_active_account(session, db_task, hint)
                        active_account_label = hint
                        logger.info(
                            "account auto-selected from memory: %r for thread %s",
                            hint,
                            thread_key,
                        )
                    elif n_accounts > 1:
                        # Multiple accounts, no hint → ask the owner.
                        account_ask_needed = True
        gate_kwargs = self._session_kwargs(
            thread_key=thread_key,
            tier=tier,
            db_id=db_id,
            from_label=from_label,
            surface=surface,
            active_account_label=active_account_label,
            account_ask_needed=account_ask_needed,
        )
        # Guests run on the guest model (Sonnet, never Opus); the owner on theirs —
        # swapped for the cheaper budget model while the cycle is downgraded (M9), or
        # reopened on Opus if the thread was escalated (Task.model, M11).
        if tier == "owner":
            model = await self._owner_session_model(persisted=persisted_model)
        else:
            model = self._guest_model or self._owner_model
        rt = _RunningTask(
            thread_key=thread_key,
            db_id=db_id,
            session=self._session_factory_sdk(
                model=model, resume=resume, **gate_kwargs
            ),
            queue=asyncio.Queue(),
            tier=tier,
            is_casual=is_casual,
            surface=surface,
            model=model,
        )
        self._tasks[thread_key] = rt
        return rt

    def _session_kwargs(
        self,
        *,
        thread_key: str,
        tier: str,
        db_id: int,
        from_label: str | None = None,
        surface: Surface = Surface.DM,
        active_account_label: str | None = None,
        account_ask_needed: bool = False,
    ) -> dict[str, Any]:
        """Assemble the SDK session kwargs (gate + memory/tool scoping) for a thread.

        Shared by :meth:`_ensure_task`, casual reseed, and :meth:`branch` so a session
        is built the same way wherever it originates (no per-call drift). ``resume`` /
        ``fork_session`` are layered on by the caller — they vary per origin. The owner
        and guest tool surfaces diverge sharply (tier isolation by construction), so
        each is wired by its own helper; ``from_label`` is the guest's display name.

        ``active_account_label`` is the thread's current active Google account (from
        :attr:`Task.active_account`, issue #46).  When set, the calendar MCP server
        config is stamped with an ``X-Account-Label`` header so the server routes the
        request to the right credential — without the model ever seeing or passing an
        account argument.

        ``account_ask_needed`` (issue #49): when ``True``, the owner session's system
        prompt includes the account-selection guidance
        (:data:`~chief.tools.google.account_selection.ACCOUNT_SELECTION_GUIDANCE`)
        so the model asks which account to use before touching any Google API.  Only
        set when there are multiple accounts, no thread binding, and no memory hint.
        """
        can_use_tool, hooks = self._build_gate(
            task_id=db_id, thread_key=thread_key, tier=tier, surface=surface
        )
        gate_kwargs: dict[str, Any] = {}
        if can_use_tool is not None or hooks is not None:
            gate_kwargs = {"can_use_tool": can_use_tool, "hooks": hooks}
        # Always refuse the built-in shell at the SDK layer — the SDK-side mate of the
        # gate's hard DENY (see gate.classify), wired independently of memory so a
        # future memory=None path can't silently restore the in-core shell. Per-service
        # Google deferred ops (delete) are appended below, where services are in scope.
        disallowed_tools = list(DISALLOWED_BUILTINS)
        gate_kwargs["disallowed_tools"] = disallowed_tools
        if self._memory is None:
            return gate_kwargs
        if tier == "owner":
            self._wire_owner_session(
                gate_kwargs,
                thread_key,
                disallowed_tools,
                surface,
                active_account_label=active_account_label,
                account_ask_needed=account_ask_needed,
            )
        else:
            self._wire_guest_session(gate_kwargs, from_label)
        return gate_kwargs

    def _wire_owner_session(
        self,
        gate_kwargs: dict[str, Any],
        thread_key: str,
        disallowed_tools: list[str],
        surface: Surface = Surface.DM,
        *,
        active_account_label: str | None = None,
        account_ask_needed: bool = False,
    ) -> None:
        """Wire the owner's full surface: memory, web, Google, shell, admin, skills.

        ``active_account_label`` is the thread's active Google account (issue #46).
        When set, the calendar service config is built with an ``X-Account-Label``
        header so the server selects the right credential per request — transparent
        to the model.

        ``account_ask_needed`` (issue #49): when ``True``, appends
        :data:`~chief.tools.google.account_selection.ACCOUNT_SELECTION_GUIDANCE` to the
        system prompt so the model asks the owner which account to use before touching
        any Google API — only active when multiple accounts are registered, the thread
        has no binding, and memory provides no hint.
        """
        assert self._memory is not None
        services = self._build_services_with_account(active_account_label)
        workspace_on = self._workspace_dir is not None
        shell_on = self._shell_service is not None
        admin = self._guest_admin_service
        list_accounts = self._list_accounts_service
        set_account = self._set_account_service
        add_account = self._add_account_service
        # Packaged skills are owner-only (M10): a guest session never reaches here, so
        # the plugin + enable-list ride only the owner's options. Both guard on a
        # configured plugin path so an enabled-but-unwired flag stays inert (no error).
        skills_on = self._skills_enabled and self._skills_plugin_path is not None
        allowed = list(MEMORY_TOOLS) + list(WEB_META_TOOLS)
        if workspace_on:
            # Write/Edit join the allow-list; gate confines them to memory ∪ workspace.
            allowed += list(WORKSPACE_TOOLS)
        for svc in services:
            # Reads only — writes stay off the allow-list so they reach approval.
            allowed += list(svc.read_tools)
        if admin is not None:
            # Owner-initiated, reversible → pre-approved (no card) to block/mute guests.
            allowed.append(admin.tool_name)
        if list_accounts is not None:
            # Pure read — no approval card; guests never see this service.
            allowed.append(list_accounts.tool_name)
        if set_account is not None:
            # Owner-initiated — pre-approved (no card); guests never see this service.
            allowed.append(set_account.tool_name)
        if add_account is not None:
            # Owner-initiated runtime account add — pre-approved (no card); guests
            # never see this service.
            allowed.append(add_account.tool_name)
        schedule = self._schedule_service
        if schedule is not None:
            # The benign schedule tools (message/wakeup + list/cancel) only mint safe
            # actions, so they're pre-approved (no card) — extra_read_only in
            # _build_gate keeps the gate hook from carding them despite the allow entry.
            allowed += list(schedule.tool_names)
        system_prompt = build_system_prompt(
            tier="owner",
            memory=self._memory,
            owner_name=self._owner_name,
            google_services=frozenset(svc.name for svc in services),
            owner_tz=self._owner_tz,
            workspace_enabled=workspace_on,
            shell_enabled=shell_on,
            guest_admin_enabled=admin is not None,
            skills=self._default_skills if skills_on else (),
            platform=self._platform,
        )
        if surface is Surface.GROUP:
            # Same owner toolset, but a reminder the room is public and approvals are
            # DM'd — so the model neither leaks private context nor waits on a card it
            # can't see in the group.
            system_prompt = f"{system_prompt}\n\n{GROUP_MODE_NOTE}"
        if account_ask_needed:
            # No per-thread binding and no memory hint with multiple registered accounts
            # (issue #49): instruct the model to ask which account before any Google
            # call, then bind via set_account — never silently guess.
            system_prompt = f"{system_prompt}\n\n{ACCOUNT_SELECTION_GUIDANCE}"
        gate_kwargs.update(
            system_prompt=system_prompt,
            cwd=self._memory_dir,
            allowed_tools=allowed,
        )
        if skills_on:
            # The plugin manifest provides the SKILL.md dirs; the skills= filter scopes
            # exactly the curated set on (the SDK turns on the Skill tool itself).
            gate_kwargs["plugins"] = [
                {"type": "local", "path": self._skills_plugin_path}
            ]
            gate_kwargs["skills"] = list(self._default_skills)
        # Each Google container (docker/mcp-*) + the in-process shell server. Google
        # writes and the shell tool are absent from allowed_tools, so they reach
        # can_use_tool → approval; Google deferred ops are blocked. Google reads are
        # pre-approved above + ALLOWed by the gate's extra_read_only.
        mcp_servers: dict[str, Any] = {
            svc.server_name: svc.server_config() for svc in services
        }
        if shell_on:
            shell = self._shell_service
            assert shell is not None  # narrowed by shell_on
            # Built per task: the bash closure addresses THIS task's own shell (keyed by
            # thread_key), since an in-process MCP handler gets no caller context. Kept
            # out of allowed_tools → routes to can_use_tool → ASK.
            mcp_servers[shell.server_name] = shell.server_config(session_key=thread_key)
        if admin is not None:
            mcp_servers[admin.server_name] = admin.server_config()
        if list_accounts is not None:
            mcp_servers[list_accounts.server_name] = list_accounts.server_config()
        if set_account is not None:
            # Per-thread: the closure addresses THIS thread's DB row.
            mcp_servers[set_account.server_name] = set_account.server_config(
                thread_key=thread_key
            )
        if add_account is not None:
            # Thread-agnostic: writes a global token file the registry re-scans.
            mcp_servers[add_account.server_name] = add_account.server_config()
        if schedule is not None:
            mcp_servers[schedule.server_name] = schedule.server_config()
        bash_schedule = self._schedule_bash_service
        if bash_schedule is not None:
            # Gated: register the server but keep its tool_names OFF the allow-list, so
            # each mint routes through can_use_tool → ASK (same as the shell tool — the
            # ungated fire it sets up is the gated act).
            mcp_servers[bash_schedule.server_name] = bash_schedule.server_config()
        if mcp_servers:
            gate_kwargs["mcp_servers"] = mcp_servers
        # Google deferred ops (delete) refused on top of the always-disallowed built-in
        # shell wired above.
        for svc in services:
            disallowed_tools += list(svc.deferred_tools)

    def _wire_guest_session(
        self, gate_kwargs: dict[str, Any], from_label: str | None
    ) -> None:
        """Wire ONLY the guest receptionist surface — never the owner's memory or cwd.

        take-a-message relays to the Front Desk; the narrowed calendar (when wired) adds
        free/busy reads + an approval-gated booking. No memory file tools, no memory
        cwd, no web/shell/workspace — tier isolation by construction.
        """
        assert self._memory is not None
        front_desk = self._front_desk_thread_key
        allowed: list[str] = []
        mcp_servers: dict[str, Any] = {}
        if front_desk is not None:

            async def relay(text: str) -> None:
                await self._io.send(front_desk, text)

            guest_svc = GuestService(relay=relay, from_label=from_label or "a visitor")
            allowed.append(guest_svc.tool_name)
            mcp_servers[guest_svc.server_name] = guest_svc.server_config()
        cal = self._guest_calendar_service
        if cal is not None:
            allowed += list(cal.read_tools)  # free/busy pre-approved; create-event ASKs
            mcp_servers[cal.server_name] = cal.server_config()
        gate_kwargs.update(
            system_prompt=build_system_prompt(
                tier="guest",
                memory=self._memory,
                owner_name=self._owner_name,
                google_services=(
                    frozenset({cal.name}) if cal is not None else frozenset()
                ),
                owner_tz=self._owner_tz,
                platform=self._platform,
            ),
            allowed_tools=allowed,
        )
        # Hard-deny the owner's built-in file/web/write tools at the SDK layer too — not
        # just absent from allowed_tools but refused outright (like the built-in shell),
        # so a guest can never read files or the web even if a call reaches the gate.
        gate_kwargs["disallowed_tools"] = gate_kwargs["disallowed_tools"] + GUEST_DENIED
        if mcp_servers:
            gate_kwargs["mcp_servers"] = mcp_servers

    def _build_services_with_account(
        self, active_account_label: str | None
    ) -> tuple[GoogleService, ...]:
        """Return the Google services tuple, with all Google MCP servers stamped with
        an ``X-Account-Label`` header when a per-thread account is active
        (issue #46/#47/#48).

        Calendar, Drive, Sheets, and chief-owned Gmail are each rebuilt with a
        per-session headers dict so every HTTP call the SDK sends to those servers
        carries the label — the server uses it to select the right credential per
        request.

        When ``active_account_label`` is ``None`` (no binding set for the thread),
        service configs carry no header and each server falls back to the default
        (first / single-account) credential, keeping backward compat.
        """
        if not active_account_label:
            return self._google_services
        headers = {"X-Account-Label": active_account_label}
        result: list[GoogleService] = []
        for svc in self._google_services:
            if svc.name == "calendar":
                result.append(calendar_mcp.service(svc.url, headers=headers))
            elif svc.name == "drive":
                result.append(drive_mcp.service(svc.url, headers=headers))
            elif svc.name == "sheets":
                result.append(sheets_mcp.service(svc.url, headers=headers))
            elif svc.name == "gmail_chief":
                result.append(gmail_mcp.chief_service(svc.url, headers=headers))
            else:
                result.append(svc)
        return tuple(result)

    def _approval_route(self, *, tier: str, thread_key: str, surface: Surface) -> str:
        """Where this session's approval card lands; raise if there's no private route.

        Owner work approves in-thread — except on a GROUP surface, where the card must
        never post in the shared room and is DM'd to the owner (``owner_inbox``) instead
        (M11). A guest's card routes to the Front Desk; a group guest with no Front Desk
        falls back to the owner DM, never back into the guest's own message (the leak M6
        forbids). Every GROUP path **fails closed**: with no private inbox configured we
        raise rather than silently routing a card into the public room.
        """
        if tier == "owner":
            if surface is not Surface.GROUP:
                return thread_key
            if self._owner_inbox is None:
                raise RuntimeError(
                    "owner group approval has no private route — set "
                    "primary_thread_key (wired to owner_inbox) so the card can't "
                    "post in the group."
                )
            return self._owner_inbox
        if self._front_desk_thread_key is not None:
            return self._front_desk_thread_key
        if surface is Surface.GROUP and self._owner_inbox is not None:
            return self._owner_inbox
        raise RuntimeError(
            "guest approval has no Front Desk route — set front_desk_thread_key"
        )

    def _build_gate(
        self, *, task_id: int, thread_key: str, tier: str, surface: Surface = Surface.DM
    ) -> tuple[CanUseTool | None, dict[HookEvent, list[HookMatcher]] | None]:
        """Bind this session's gate callbacks, or ``(None, None)`` if unwired.

        Returns the ``can_use_tool`` callback and the ``PreToolUse`` hook map for the
        SDK options. The status hooks flip the task ``waiting``/``running`` around an
        approval so ``/tasks`` reflects a blocked turn.
        """
        if self._policy is None or self._approvals is None or self._audit is None:
            return None, None
        # Calendar reads ALLOW with no card; the names live with each MCP catalog so the
        # gate stays MCP-agnostic. The owner sees the full Google set; a guest sees only
        # the narrowed calendar (free/busy), so its booking write still reaches a card.
        if tier == "owner":
            services: tuple[GoogleService, ...] = self._google_services
        elif self._guest_calendar_service is not None:
            services = (self._guest_calendar_service,)
        else:
            services = ()
        extra_read_only = frozenset(
            tool for svc in services for tool in svc.read_tools
        )
        # The owner's guest-admin tool (block/mute/unblock) is owner-initiated and
        # reversible → ALLOW with no card. The gate's extra_read_only set is its "allow
        # without a card" lever, so reuse it (the tool mutates state, but it's the
        # owner's own command — never an approval prompt to themselves).
        if tier == "owner" and self._guest_admin_service is not None:
            extra_read_only = extra_read_only | {
                self._guest_admin_service.tool_name
            }
        # The benign schedule tools are owner-initiated and only mint safe actions →
        # ALLOW with no card. Like guest-admin, reuse the gate's "allow without a card"
        # lever so the PreToolUse hook doesn't card them despite their allow-list entry.
        # The gated schedule_bash tools are deliberately absent here → they reach ASK.
        if tier == "owner" and self._schedule_service is not None:
            extra_read_only = extra_read_only | set(
                self._schedule_service.tool_names
            )
        # set_account is owner-initiated (sets the per-thread active Google account) →
        # ALLOW with no card. It mutates only per-thread DB state the owner controls.
        if tier == "owner" and self._set_account_service is not None:
            extra_read_only = extra_read_only | {self._set_account_service.tool_name}
        # add_account is owner-initiated (runtime account registration via consent) →
        # ALLOW with no card. Guests never receive it.
        if tier == "owner" and self._add_account_service is not None:
            extra_read_only = extra_read_only | {self._add_account_service.tool_name}
        # Owner work approves in-thread (a group DMs the owner); a guest-originated
        # approval routes to the Front Desk. A guest with no route is a hard error —
        # never silently self-route a card back into the guest's own DM.
        route = self._approval_route(
            tier=tier, thread_key=thread_key, surface=surface
        )
        # Both file roots are owner-only. A guest has no file tools at all, so its gate
        # carries neither a memory nor a workspace root — total isolation by
        # construction (matches the cwd=None treatment in _wire_guest_session).
        owner = tier == "owner"
        memory_dir = self._memory_dir if owner else None
        workspace_dir = self._workspace_dir if owner else None
        hook = build_pretool_hook(
            thread_key=thread_key,
            tier=tier,
            policy=self._policy,
            audit=self._audit,
            memory_dir=memory_dir,
            workspace_dir=workspace_dir,
            extra_read_only=extra_read_only,
        )

        async def on_waiting() -> None:
            await self._set_status_by_thread(thread_key, WAITING)

        async def on_running() -> None:
            await self._set_status_by_thread(thread_key, RUNNING)

        can_use_tool = build_can_use_tool(
            task_id=task_id,
            thread_key=thread_key,
            tier=tier,
            route=route,
            policy=self._policy,
            approvals=self._approvals,
            audit=self._audit,
            on_waiting=on_waiting,
            on_running=on_running,
            memory_dir=memory_dir,
            workspace_dir=workspace_dir,
            extra_read_only=extra_read_only,
        )
        hooks: dict[HookEvent, list[HookMatcher]] = {
            "PreToolUse": [HookMatcher(hooks=[hook])]
        }
        # Screenshot delivery: owner sessions with a configured screenshots dir get
        # a PostToolUse hook that reads the saved file and delivers it via send_file.
        # Guest sessions never have browser tools — owner-only by construction.
        if tier == "owner" and self._screenshots_dir is not None:
            screenshot_hook = build_screenshot_hook(
                thread_key=thread_key,
                io=self._io,
                screenshots_dir=self._screenshots_dir,
            )
            hooks["PostToolUse"] = [HookMatcher(hooks=[screenshot_hook])]
        return can_use_tool, hooks

    async def _submit(self, task: _RunningTask, turn: Turn) -> None:
        self._cancel_idle(task)
        if task.generating:
            if (
                await self._stop_intent(turn.text, model=self._classifier_model)
                and task.generating
            ):
                await task.session.interrupt()
        elif task.consumer is None or task.consumer.done():
            task.consumer = asyncio.create_task(self._consume(task))
        task.queue.put_nowait(turn)

    async def _consume(self, task: _RunningTask) -> None:
        while True:
            turn = await task.queue.get()
            await self._run_turn(task, turn)

    # ---- budget enforcement (M9) ----------------------------------------

    async def _owner_session_model(self, *, persisted: str | None = None) -> str:
        """The model a new owner session opens on.

        A persisted Opus escalation (``Task.model``, M11) wins outright: explicit
        escalation overrides an active budget downgrade so a reopened thread comes back
        on Opus. Otherwise the cheaper budget model while the cycle is ``downgraded``
        (M9), else the configured owner model.
        """
        if persisted == self._owner_model_opus:
            return self._owner_model_opus
        if self._budget is not None and self._budget_downgrade_model is not None:
            if await self._budget.mode() == usage.MODE_DOWNGRADED:
                return self._budget_downgrade_model
        return self._owner_model

    async def _budget_admits(self) -> bool:
        """False when the cycle is paused at budget — the turn must be skipped without
        spending. Reminds the owner once per pause episode that turns are blocked."""
        if self._budget is None:
            return True
        if await self._budget.mode() != usage.MODE_PAUSED:
            self._paused_ack_sent = False
            return True
        if not self._paused_ack_sent and self._owner_inbox is not None:
            self._paused_ack_sent = True
            await self._io.send(self._owner_inbox, PAUSED_BUDGET_ACK)
        return False

    async def _record_spend(self, task: _RunningTask) -> None:
        """Roll this turn's SDK cost into the cycle total; a hard rate-limit rejection
        is treated like exhaustion (pause + ask)."""
        if self._budget is None:
            return
        await self._budget.record(task.session.last_cost_usd)
        if task.session.last_rate_limit_status == "rejected":
            await self._budget.note_rate_limited()

    async def downgrade_live_sessions(self) -> None:
        """Switch every live owner session to the budget downgrade model (M9).

        Invoked when the owner taps **Downgrade** on the budget card; new sessions
        already pick the model up via :meth:`_owner_session_model`. No-op when no
        downgrade model is configured (budget disabled).

        An Opus-pinned thread is **skipped** (M11): an explicit escalation overrides a
        budget downgrade — the owner chose to spend faster and was warned — so a
        Downgrade tap leaves it on Opus (live and persisted stay in agreement, matching
        the reopen precedence in :meth:`_owner_session_model`; ``/sonnet`` drops it).
        """
        if self._budget_downgrade_model is None:
            return
        for task in list(self._tasks.values()):
            if task.tier != "owner" or task.model == self._owner_model_opus:
                continue
            await task.session.set_model(self._budget_downgrade_model)
            task.model = self._budget_downgrade_model

    # ---- Opus escalation (M11) ------------------------------------------

    async def escalate(self, thread_key: str) -> str:
        """Switch the owner thread to Opus now and persist it (``/opus``, M11).

        Persists ``Task.model`` so the thread reopens on Opus after a restart, and — if
        a session is live — switches it mid-thread so the next turn (including the one
        that triggered an auto-detect card) runs on Opus. Explicit escalation overrides
        an active budget downgrade; the reply warns that Opus burns the credit faster.
        """
        async with self._session_factory() as session:
            db = await get_or_create_task(
                session, platform=self._platform, thread_key=thread_key, tier="owner"
            )
            await set_task_model(session, db, self._owner_model_opus)
        task = self._tasks.get(thread_key)
        if task is not None:
            await task.session.set_model(self._owner_model_opus)
            task.model = self._owner_model_opus
            task.auto_escalate_suppressed = False
        note = ""
        if (
            self._budget is not None
            and await self._budget.mode() == usage.MODE_DOWNGRADED
        ):
            note = f" {OPUS_BUDGET_NOTE}"
        return f"{OPUS_CONFIRM}{note}"

    async def revert(self, thread_key: str) -> str:
        """Clear an Opus escalation; drop back to the default model (``/sonnet``, M11).

        Clears the persisted ``Task.model`` and, if a session is live, switches it back:
        the budget-downgrade model when the cycle is downgraded, else the owner model.
        """
        async with self._session_factory() as session:
            db = await get_task(
                session, platform=self._platform, thread_key=thread_key
            )
            if db is not None:
                await set_task_model(session, db, None)
        task = self._tasks.get(thread_key)
        if task is not None:
            model = await self._owner_session_model()
            await task.session.set_model(model)
            task.model = model
            task.auto_escalate_suppressed = False
        return SONNET_CONFIRM

    async def _maybe_auto_escalate(self, task: _RunningTask, text: str) -> None:
        """Ask to escalate a complex owner turn to Opus, if opt-in auto-detect is on.

        Runs at the start of a turn (in the detached consumer, never inline in dispatch:
        PTB processes updates sequentially, so blocking dispatch on the approval future
        would deadlock the very button-tap that resolves it). Cheap guards short-circuit
        before the Haiku classifier: off, not the owner, already on Opus, already denied
        this task, or no approval channel. A denial suppresses re-asking for the task.
        """
        if (
            not self._opus_auto_detect
            or task.tier != "owner"
            or task.model == self._owner_model_opus
            or task.auto_escalate_suppressed
            or self._approvals is None
        ):
            return
        if not await self._is_complex(text, model=self._classifier_model):
            return
        route = self._approval_route(
            tier="owner", thread_key=task.thread_key, surface=task.surface
        )
        approved = await self._approvals.request(
            task_id=task.db_id,
            thread_key=task.thread_key,
            tier="owner",
            tool_name=OPUS_ESCALATION_KIND,
            tool_input={"reason": _title(text)},
            route=route,
        )
        if approved:
            # Surface escalate()'s confirmation (incl. the budget warning when the cycle
            # is downgraded) so the auto-detect path warns the same as explicit /opus.
            await self._io.send(task.thread_key, await self.escalate(task.thread_key))
        else:
            task.auto_escalate_suppressed = True

    def _owner_streams_per_block(self, task: _RunningTask) -> bool:
        """True when the owner's reply should be streamed one block at a time.

        Scoped to owner home/DM (issue #64): GROUP turns and all guest turns
        accumulate into a single message (unchanged behaviour for this slice).
        """
        return task.tier == "owner" and task.surface in (Surface.HOME, Surface.DM)

    async def _run_turn(self, task: _RunningTask, turn: Turn) -> None:
        if not await self._budget_admits():
            return  # paused at budget — skip without spending (owner already nudged)
        await self._maybe_auto_escalate(task, turn.text)
        ack = asyncio.create_task(self._ack_after_grace(task))
        task.transcript.append(("owner", turn.text))
        try:
            async with self._semaphore:
                task.generating = True
                await self._set_status(task, RUNNING)
                # For owner home/DM, each text block is delivered immediately as it
                # arrives (per-block streaming, issue #64). For group turns and guest
                # sessions the blocks are accumulated into a single joined message,
                # preserving unchanged behaviour for those surfaces.
                per_block = self._owner_streams_per_block(task)
                block_parts: list[str] = []
                # Watchdog: a turn whose stream never reaches a terminal result (wedged
                # SDK / hung tool call) must not pin the semaphore and generating=True
                # forever. The timeout cancels the loop; exiting the semaphore block
                # frees the slot, and the TimeoutError handler tears the session down.
                async with asyncio.timeout(self._turn_timeout):
                    async for event in task.session.run_turn(
                        turn.text, turn.attachments
                    ):
                        if task.cancelled:
                            break  # interrupted — stop streaming its milestones
                        if isinstance(event, Final):
                            if per_block:
                                await self._emit_final(task, event.text)
                            else:
                                block_parts.append(event.text)
                        else:
                            await self._io.send(task.thread_key, f"· {event.text}")
                ack.cancel()
                if task.cancelled:
                    return
                if not per_block:
                    # Accumulated path (group/guest): join and emit as one message,
                    # reproducing the original single-Final behaviour.
                    joined = "".join(block_parts).strip() or NO_REPLY
                    await self._emit_final(task, joined)
                if task.session.session_id:
                    await self._set_session_id(task, task.session.session_id)
                await self._record_spend(task)
                await self._set_status(task, OPEN)
                # Commit memory dir after the turn's writes settle (#22/#29). The
                # versioner self-serializes concurrent callers; it also skips empty
                # commits, so no-op turns cost one git status check (< 1 ms).
                await self._versioner.commit("chief: memory auto-save")
                # Only a clean turn re-arms the idle→archive timer.
                self._arm_idle(task)
        except TimeoutError:
            ack.cancel()
            logger.warning("task turn timed out", extra={"thread_key": task.thread_key})
            await self._set_status(task, FAILED)
            await self._io.send(task.thread_key, TURN_TIMEOUT_NOTE)
            await self._reset_session(task)
        except Exception:
            logger.exception("task turn failed", extra={"thread_key": task.thread_key})
            await self._set_status(task, FAILED)
            await self._io.send(task.thread_key, "⚠️ that task hit an error.")
        finally:
            ack.cancel()
            task.generating = False

    async def _reset_session(self, task: _RunningTask) -> None:
        """Best-effort teardown of a wedged session so the next turn reconnects fresh.

        The watchdog fired because the turn never terminated, so ``interrupt`` may also
        hang on the wedged control stream; ``aclose`` (subprocess disconnect) then gets
        a fresh CLI next turn. Both are time-boxed and guarded — this teardown runs on
        the consumer loop, so a hung step here would re-freeze the task we just rescued.
        """
        for label, teardown in (
            ("interrupt", task.session.interrupt),
            ("aclose", task.session.aclose),
        ):
            try:
                async with asyncio.timeout(_RESET_TIMEOUT):
                    await teardown()
            except Exception:
                logger.debug(
                    "%s during turn-timeout reset failed", label, exc_info=True
                )

    async def _emit_final(self, task: _RunningTask, text: str) -> None:
        """Deliver the final reply: a Markdown file when long, else split messages (M8).

        Long or code-heavy replies go out as a timestamped ``.md`` attachment with a
        short note, instead of a wall of mid-token hard cuts; everything else flows
        through :meth:`TaskIO.send`, which boundary-splits to the platform cap.
        """
        if should_send_as_file(text, self._message_limit):
            await self._io.send_file(
                task.thread_key,
                reply_filename(),
                text.encode("utf-8"),
                caption=FILE_REPLY_NOTE,
            )
        else:
            await self._io.send(task.thread_key, text)
        task.transcript.append(("chief", text))

    async def _ack_after_grace(self, task: _RunningTask) -> None:
        try:
            await asyncio.sleep(self._grace_seconds)
        except asyncio.CancelledError:
            return
        await self._io.send(task.thread_key, WORKING_ACK)

    def _arm_idle(self, task: _RunningTask) -> None:
        if task.cancelled or self._tasks.get(task.thread_key) is not task:
            return  # torn down or cancelled — don't resurrect a timer
        self._cancel_idle(task)
        # The lanes diverge here: a casual ``:0`` channel self-compacts and stays OPEN;
        # a real task thread archives and dies.
        idle = self._idle_then_compact if task.is_casual else self._idle_then_archive
        task.idle_handle = asyncio.create_task(idle(task))

    def _cancel_idle(self, task: _RunningTask) -> None:
        if task.idle_handle is not None:
            task.idle_handle.cancel()
            task.idle_handle = None

    async def _idle_then_archive(self, task: _RunningTask) -> None:
        try:
            await asyncio.sleep(self._idle_archive_seconds)
        except asyncio.CancelledError:
            return
        if task.cancelled or self._tasks.get(task.thread_key) is not task:
            return
        task.cancelled = True
        await self._set_status(task, DONE)
        await self._io.archive_thread(task.thread_key)
        await self._cancel_consumer(task)
        await task.session.aclose()
        # Pop last: keep the slot (cancelled, idle_handle live) so a reopening
        # message awaits this teardown in _ensure_task instead of racing it.
        self._tasks.pop(task.thread_key, None)
        logger.info("task archived on idle", extra={"thread_key": task.thread_key})

    async def _idle_then_compact(self, task: _RunningTask) -> None:
        """Casual-lane idle handler: self-compact in place of archiving (stays OPEN).

        Mirrors :meth:`_idle_then_archive`'s guard/teardown shape but swaps the archive
        for a summarize-and-reseed, so the casual channel keeps living — now resumed
        from a compact brief of its own history instead of an ever-growing transcript.
        The status is left OPEN and the thread is never archived.
        """
        try:
            await asyncio.sleep(self._compaction_idle_seconds)
        except asyncio.CancelledError:
            return
        if task.cancelled or self._tasks.get(task.thread_key) is not task:
            return
        task.cancelled = True
        try:
            await self._summarize_and_reseed(task)
            logger.info(
                "casual channel compacted on idle",
                extra={"thread_key": task.thread_key},
            )
        except Exception:
            # A failed compaction leaves the persisted id on the old session, so the
            # next message resumes the full transcript — no compaction this round.
            logger.exception(
                "compaction failed", extra={"thread_key": task.thread_key}
            )
        await self._cancel_consumer(task)
        await task.session.aclose()
        # Pop last (mirrors archive): a reopening message awaits this teardown in
        # _ensure_task, then resumes the freshly reseeded id.
        self._tasks.pop(task.thread_key, None)

    async def _summarize_and_reseed(self, task: _RunningTask) -> str:
        """Brief the live casual session, then reseed onto a fresh one from that brief.

        Replicates ``/compact``'s behaviour (the SDK exposes no programmatic trigger):
        summarize the full live transcript, seed a brand-new session with the brief, and
        point the task's persisted ``sdk_session_id`` at that small reseeded session.
        Returns the brief. The old session is torn down by the caller.
        """
        summary = await self._run_silent_turn(task.session, COMPACT_PROMPT)
        # Carry the active account forward into the reseeded session so calendar
        # calls in the new session still hit the right credential (issue #46).
        active_account_label: str | None = None
        if self._set_account_service is not None:
            active_account_label = (
                await self._set_account_service.get_active_account_label(
                    task.thread_key
                )
            )
        gate_kwargs = self._session_kwargs(
            thread_key=task.thread_key,
            tier=task.tier,
            db_id=task.db_id,
            active_account_label=active_account_label,
        )
        fresh = self._session_factory_sdk(
            model=self._owner_model, resume=None, **gate_kwargs
        )
        try:
            await self._run_silent_turn(fresh, PRIME_TEMPLATE.format(summary=summary))
            reseeded_id = fresh.session_id
        finally:
            await fresh.aclose()
        if reseeded_id is not None:
            await self._set_session_id(task, reseeded_id)
        return summary

    @staticmethod
    async def _run_silent_turn(session: SessionProto, text: str) -> str:
        """Drive one turn on ``session`` silently (not surfaced); return its text.

        The session now yields one ``Final`` per text block (issue #64); we accumulate
        all of them so the caller (compaction/prime) sees the full turn text, not just
        the last block.
        """
        parts: list[str] = []
        async for event in session.run_turn(text):
            if isinstance(event, Final):
                parts.append(event.text)
        return "".join(parts)

    async def branch(self, thread_key: str, title: str) -> str:
        """Promote a casual chat into a full-memory thread carrying its current context.

        Creates a new tracked thread and forks the casual channel's live SDK session
        into it (``resume`` + ``fork_session``), so the new thread starts with the
        casual channel's full context while the casual channel compacts independently.
        The forked session's new id is captured + persisted on the new thread's first
        turn (like any session). Returns the new ``thread_key``.
        """
        casual_resume = await self._casual_session_id(thread_key)
        new_key = await self._io.create_thread(
            like_thread_key=thread_key, title=title
        )
        async with self._session_factory() as session:
            db = await get_or_create_task(
                session,
                platform=self._platform,
                thread_key=new_key,
                tier="owner",
                title=title,
            )
            db_id = db.id
        # Carry the active account into the branched thread; it may be None if the
        # casual channel had no account bound (backward compat, issue #46).
        active_account_label: str | None = None
        if self._set_account_service is not None:
            active_account_label = (
                await self._set_account_service.get_active_account_label(new_key)
            )
        gate_kwargs = self._session_kwargs(
            thread_key=new_key,
            tier="owner",
            db_id=db_id,
            active_account_label=active_account_label,
        )
        rt = _RunningTask(
            thread_key=new_key,
            db_id=db_id,
            session=self._session_factory_sdk(
                model=self._owner_model,
                resume=casual_resume,
                # Fork only when there's a session to fork; an empty casual (no turn
                # yet) has no context to carry, so the new thread just starts fresh.
                fork_session=casual_resume is not None,
                **gate_kwargs,
            ),
            queue=asyncio.Queue(),
            tier="owner",
            model=self._owner_model,
        )
        self._tasks[new_key] = rt
        return new_key

    async def _casual_session_id(self, thread_key: str) -> str | None:
        """The casual channel's freshest resumable id: live session over the DB row.

        A live ``_RunningTask`` carries the id from its last turn's stream, at least as
        fresh as the DB (written post-turn), so a fork mid-flight doesn't use a stale
        id. Falls back to the persisted id when no task is live (e.g. post-compaction).
        """
        live = self._tasks.get(thread_key)
        if live is not None and live.session.session_id is not None:
            return live.session.session_id
        async with self._session_factory() as session:
            casual = await get_task(
                session, platform=self._platform, thread_key=thread_key
            )
            return casual.sdk_session_id if casual is not None else None

    async def _stop_task(self, task: _RunningTask) -> None:
        """Tear down: cancel the timers + consumer (awaited) and close the session."""
        task.cancelled = True
        self._cancel_idle(task)
        self._tasks.pop(task.thread_key, None)
        await self._cancel_consumer(task)
        await task.session.aclose()

    @staticmethod
    async def _cancel_consumer(task: _RunningTask) -> None:
        """Cancel the consumer and await it so any open db session unwinds cleanly."""
        consumer = task.consumer
        if consumer is None:
            return
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass

    # ---- persistence helpers --------------------------------------------

    async def _set_status(self, task: _RunningTask, status: str) -> None:
        await self._set_status_by_thread(task.thread_key, status)

    async def _set_status_by_thread(self, thread_key: str, status: str) -> None:
        async with self._session_factory() as session:
            db = await get_task(
                session, platform=self._platform, thread_key=thread_key
            )
            if db is not None:
                await set_status(session, db, status)

    async def _set_session_id(self, task: _RunningTask, sdk_session_id: str) -> None:
        async with self._session_factory() as session:
            db = await get_task(
                session, platform=self._platform, thread_key=task.thread_key
            )
            if db is not None:
                await set_session_id(session, db, sdk_session_id)
