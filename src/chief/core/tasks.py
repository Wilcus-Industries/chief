"""Task engine — the core loop (DESIGN: task execution engine).

A conversation *is* a task: one persistent :class:`TaskSession` per ``thread_key``, kept
in a registry. Each task owns an input queue drained by a single consumer, so its turns
run in order. Concurrency across tasks is bounded by a semaphore (only *generating*
turns hold a slot — idle sessions cost nothing). Behaviours:

- **Output.** All turn output — streamed reply blocks, milestones, error notes — goes
  through :class:`TaskIO`, so every surface shares one code path.
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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from copilot import ProviderConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.base import (
    FILE_REPLY_NOTE,
    Attachment,
    Surface,
    reply_filename,
    should_send_as_file,
)
from ..gate.approvals import OPUS_ESCALATION_KIND, ApprovalManager
from ..gate.blacklist import Blacklist
from ..gate.gate import (
    BUILTIN_SHELL_TOOLS,
    FILE_OP_TOOLS,
    WRITE_OP_TOOLS,
    build_can_use_tool,
    build_pretool_hook,
)
from ..gate.policy import PolicyStore
from ..gate.types import CanUseTool, HookEvent, HookMatcher
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
    set_route_category,
    set_session_id,
    set_status,
    set_task_model,
    set_title,
)
from ..tools.apple import AppleService
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
from ..tools.imessage_admin import IMessageAdminService
from ..tools.routing_admin import RoutingAdminService
from ..tools.schedule import ScheduleBashService, ScheduleService
from ..tools.sheets import mcp as sheets_mcp
from ..tools.shell import ShellService
from ..tools.web import WebService
from . import classify
from .backend import CopilotBackend
from .budget import EFFECT_DOWNGRADE, premium_request_total
from .pdf import extract_pdf_attachments
from .personas import build_system_prompt
from .routing import (
    DEFAULT_CATEGORY,
    TARGET_CLASS_COPILOT,
    RoutingStore,
    RoutingTarget,
    provider_for_target,
)
from .screening import Screener, build_screening_hook, prefix_flagged

# SessionProto lives in session.py; re-exported here (``as`` = explicit re-export) so
# the engine's callers keep importing it from the TaskManager module.
from .session import NO_REPLY, Final, close_wedged_session
from .session import SessionProto as SessionProto
from .subagents import (
    build_custom_agents,
    chief_skill_directories,
    chief_skill_names,
    load_subagent_specs,
    scaffold_default_subagents,
    skill_directories_for,
)

logger = logging.getLogger("chief.core.tasks")

#: User-visible note when the per-turn watchdog fires: a turn ran past
#: ``turn_timeout`` without a terminal result (a wedged SDK stream / hung tool call),
#: so the engine tears the session down rather than freezing the task forever.
TURN_TIMEOUT_NOTE = "⚠️ that turn timed out — try again."
#: Bound on the watchdog's own teardown of a wedged session: ``interrupt`` may hang on
#: the same wedged control stream, so it is time-boxed before the session is closed. A
#: time-boxed ``aclose`` cancelled mid-teardown *leaks* the Copilot CLI subprocess
#: (#101), so the close goes through :func:`close_wedged_session`, which reaps the
#: orphan on expiry. Keeps a hung teardown from re-freezing the consumer.
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
#: Owner-facing replies for the #79 ``/route`` command. ROUTE_CONFIRM echoes the target
#: the thread now runs on; the others are the disabled / unknown-category guards.
ROUTE_CONFIRM = "🧭 Routing this thread as “{category}” → {target_class}:{model}."
ROUTING_DISABLED = "Model routing isn't enabled."
UNKNOWN_CATEGORY = "Unknown category “{category}”. Known: {known}."
#: Owner-facing replies for the session-management commands (``/close``, ``/rename``).
CLOSE_CONFIRM = "✅ Closed this thread — it's archived now."
NOTHING_TO_CLOSE = "Nothing to close here."
RENAME_CONFIRM = "✏️ Renamed this thread to “{title}”."
NOTHING_TO_RENAME = "No task here to rename."
#: Read-only file tools chief gets at M4. ``classify()`` has no ``file_path`` check, so
#: owner reads are unconfined — not fenced to the memory dir. Containment is the
#: approval blacklist, untrusted-content screening, and the audit log, not a path check
#: (footgun-catcher, not a security boundary — see ``chief/gate/blacklist.py``).
MEMORY_TOOLS = sorted(FILE_OP_TOOLS)
#: Write file tools the owner gets at M7 when the workspace is enabled — added to
#: ``allowed_tools`` unconfined (``classify()`` has no ``file_path`` check; the
#: workspace dir is a cwd convention, not a fence). Containment is the approval
#: blacklist, untrusted-content screening, and the audit log.
WORKSPACE_TOOLS = sorted(WRITE_OP_TOOLS)
#: Built-in shell tools refused outright at the SDK layer (belt-and-braces with the
#: gate's hard DENY) — chief keeps ONE shell surface, the per-task host shell
#: (``mcp__chief_shell__bash``), so its blacklist matching can't be bypassed.
DISALLOWED_BUILTINS = sorted(BUILTIN_SHELL_TOOLS)
#: The owner-only built-ins a guest must never reach. The gate classifies these as
#: read-only (ALLOW), so absence from a guest's allowed_tools is not enough — they are
#: refused at the SDK layer (disallowed_tools), the same hard-deny used for the shell.
GUEST_DENIED = sorted(set(MEMORY_TOOLS) | set(WORKSPACE_TOOLS))
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


class BudgetProto(Protocol):
    """The slice of :class:`~chief.core.budget.BudgetGate` the engine drives (#84).

    ``record`` / ``note_rate_limited`` return ``EFFECT_DOWNGRADE`` when a currency just
    crossed into ``downgraded`` (the engine then switches live sessions), else ``None``.
    """

    async def record(self, currency: str, amount: float) -> str | None: ...
    async def note_rate_limited(self, currency: str) -> str | None: ...
    async def mode(self, currency: str) -> str: ...


SessionFactory = Callable[..., SessionProto]
Classifier = Callable[..., Awaitable[bool]]
#: The category classifier seam (#79): ``(text, *, model, categories, default) -> str``.
#: Injected for testability, defaulting to
#: :func:`chief.core.classify.classify_category`.
CategoryClassifier = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class ResolvedTarget:
    """The model + BYOK provider a new owner session opens on (#79).

    ``provider`` is ``None`` for a plain Copilot-quota (or non-routed) session and the
    OpenRouter :class:`~copilot.ProviderConfig` for an ``openrouter`` target.
    """

    model: str
    provider: ProviderConfig | None = None

#: The engine builds sessions through the backend's ``create_session`` (the real seam,
#: #88). :class:`~chief.core.backend.CopilotBackend` is chief's sole harness;
#: ``app.build_engine`` constructs it and passes its ``create_session`` as
#: ``session_factory_sdk``. This default keeps a directly-constructed TaskManager (and
#: every test) on the same seam.
_default_session: SessionFactory = CopilotBackend().create_session


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
    #: The BYOK provider this live session was spawned on (#79). Non-None ⇒ an
    #: ``openrouter`` target; a budget ``set_model`` can't switch it, so such a session
    #: is skipped by :meth:`downgrade_live_sessions` (only a ``/route`` respawn changes
    #: the provider).
    provider: ProviderConfig | None = None
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
        routing: RoutingStore | None = None,
        classify_category: CategoryClassifier = classify.classify_category,
        classifier_api_key: str | None = None,
        openrouter_provider: ProviderConfig | None = None,
        routing_surface_defaults: dict[str, str] | None = None,
        default_category: str = DEFAULT_CATEGORY,
        policy: PolicyStore | None = None,
        approvals: ApprovalManager | None = None,
        audit: AuditLog | None = None,
        blacklist: Blacklist | None = None,
        front_desk_thread_key: str | None = None,
        memory: MemoryStore | None = None,
        memory_dir: str | None = None,
        owner_name: str = "the owner",
        google_services: Sequence[GoogleService] = (),
        owner_tz: str = "UTC",
        shell_service: ShellService | None = None,
        web_service: WebService | None = None,
        apple_services: Sequence[AppleService] = (),
        routing_admin_service: RoutingAdminService | None = None,
        workspace_dir: str | None = None,
        guest_model: str | None = None,
        guest_calendar_service: GoogleService | None = None,
        guest_admin_service: GuestAdminService | None = None,
        imessage_admin_service: IMessageAdminService | None = None,
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
        chief_skills_dir: str | None = None,
        subagents_enabled: bool = False,
        subagents_dir: str | None = None,
        group_context_max_messages: int = 50,
        versioner: Versioner | None = None,
        harness_versioner: Versioner | None = None,
        screenshots_dir: str | None = None,
        screener: Screener | None = None,
        screening_tools: tuple[str, ...] = (),
        screening_block: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._io = io
        self._owner_model = owner_model
        self._classifier_model = classifier_model
        self._platform = platform
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
        # Model routing (#79, part of #72): when a RoutingStore is wired, an owner
        # task's spawning message is auto-classified into a job category (on
        # classifier_model, never a routing target) and the category's target picks the
        # session's model + BYOK provider. /route overrides per task;
        # routing_surface_defaults pins a category per Surface. Inert (None) when off.
        self._routing = routing
        self._classify_category = classify_category
        # The OpenRouter key the cheap text classifiers (stop-intent / warrants-task /
        # complexity / category) authenticate their direct HTTP one-shots with (#88).
        # None ⇒ they make no HTTP call and fail safe (no interrupt, no spawn, no
        # escalation, routing falls back to the default category); the boot warns once.
        self._classifier_api_key = classifier_api_key
        self._openrouter_provider = openrouter_provider
        self._routing_surface_defaults = routing_surface_defaults or {}
        self._default_category = default_category
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
        self._blacklist = blacklist
        self._front_desk_thread_key = front_desk_thread_key
        self._memory = memory
        self._memory_dir = memory_dir
        self._owner_name = owner_name
        self._google_services = tuple(google_services)
        self._owner_tz = owner_tz
        self._shell_service = shell_service
        # chief-owned web-fetch/web-search tools (#81), owner-only. Built the shell way
        # so the single in-process ``chief_web`` MCP server reaches both backends.
        self._web_service = web_service
        # Apple ecosystem tools (#155), owner-only, darwin-gated. Resolved at boot
        # (app.resolve_apple_services): one in-process server per healthy app area
        # plus the permissions doctor; empty on Linux or when force-off.
        self._apple_services = tuple(apple_services)
        # Self-config routing tool (#83), owner-only. Edits the shared RoutingStore's
        # persisted table; its mutating verbs are gated via blacklist_tools (config.py).
        self._routing_admin_service = routing_admin_service
        self._workspace_dir = workspace_dir
        self._guest_model = guest_model
        self._guest_calendar_service = guest_calendar_service
        self._guest_admin_service = guest_admin_service
        # iMessage whitelist admin (#156), owner-only — same tier isolation as the
        # guest-admin tool; None unless the iMessage adapter is configured.
        self._imessage_admin_service = imessage_admin_service
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
        # chief-authored skills root (#106) — its leaf SKILL.md dirs join the curated
        # vendored set in the owner session's skill_directories; gated by
        # skills_enabled, owner-only.
        self._chief_skills_dir = chief_skills_dir
        # Category-routed subagents (#87), owner-only. subagents_dir is the SOLE source
        # (#103): scaffold_default_subagents seeds chief's built-ins there on first boot
        # (empty dir only), then load_subagent_specs reads it fresh at each spawn (no
        # restart). Each subagent's model is resolved through the live routing table;
        # guests never carry one (the gate lives in build_custom_agents). subagents_dir
        # is None ⇒ nothing to scaffold/load, so no subagents (production always passes
        # Settings.subagents_dir, a non-empty string).
        self._subagents_enabled = subagents_enabled
        self._subagents_dir = subagents_dir
        # Screenshot delivery (issue #34): when set, the PostToolUse hook reads
        # screenshots from this dir (shared volume) and delivers them via send_file.
        self._screenshots_dir = screenshots_dir
        # Untrusted-content screening (host-native): when a screener is wired, the
        # named tools' results get a PostToolUse injection screen and the guest relay
        # annotates flagged messages before they reach the Front Desk.
        self._screener = screener
        self._screening_tools = frozenset(screening_tools)
        self._screening_block = screening_block
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
        # Separate versioner over data/harness/ (#110): its own root and its own
        # asyncio.Lock mean harness and memory commits never collide, even though
        # both fire at the end of the same turn.
        self._harness_versioner: Versioner = (
            harness_versioner if harness_versioner is not None else NullVersioner()
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
        task = await self._ensure_task(thread_key, classify_text=text)
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
        """Route an owner message (+ media) into its task, spawning a topic when due.

        Incoming PDFs are pre-extracted to text here (#81) — the one platform- and
        backend-agnostic seam — and folded into the turn text, so neither backend has to
        carry a PDF content block (claude-agent-sdk sent a ``document`` block; the
        Copilot session dropped it). Images pass through untouched. Extraction runs off
        the event loop (``to_thread``) so a big PDF can't block other tasks' turns.
        """
        text, attachments = await asyncio.to_thread(
            extract_pdf_attachments, text, attachments
        )
        if surface is Surface.GROUP:
            # An owner-engaged group turn runs flat (a group has no forum to branch
            # into) with the full owner surface; its approval cards DM the owner.
            text = self._with_group_context(thread_key, text)
            task = await self._ensure_task(
                thread_key, surface=Surface.GROUP, classify_text=text
            )
            await self._submit(task, Turn(text=text, attachments=attachments))
            return
        is_casual = is_general
        if is_general and await self._warrants_task(
            text, model=self._classifier_model, api_key=self._classifier_api_key
        ):
            new_key = await self._io.create_thread(
                like_thread_key=thread_key, title=_title(text)
            )
            await self._io.send(thread_key, "→ Tracking that in a new topic.")
            thread_key = new_key
            is_casual = False  # a spawned topic is a real, full-memory task
        task = await self._ensure_task(
            thread_key, is_casual=is_casual, classify_text=text
        )
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

    async def close(self, thread_key: str) -> str:
        """Finish ``thread_key`` now (``/close``): mark it DONE and archive it.

        The owner-initiated twin of :meth:`_idle_then_archive` — same DONE + archive
        outcome, without waiting out the idle timer. Mirrors :meth:`cancel`'s teardown
        for a live task (interrupt a generating turn, stop timers/consumer, close the
        session); a task with no live session just flips its row. Either way the next
        message on the thread reopens it (resume), exactly like an idle archive.
        """
        task = self._tasks.get(thread_key)
        if task is None:
            async with self._session_factory() as session:
                db = await get_task(
                    session, platform=self._platform, thread_key=thread_key
                )
                if db is None or db.status in TERMINAL:
                    return NOTHING_TO_CLOSE
                await set_status(session, db, DONE)
            await self._io.archive_thread(thread_key)
            return CLOSE_CONFIRM
        task.cancelled = True
        if task.generating:
            await task.session.interrupt()
        await self._stop_task(task)
        await self._set_status(task, DONE)
        await self._io.archive_thread(thread_key)
        return CLOSE_CONFIRM

    async def rename(self, thread_key: str, title: str) -> str:
        """Retitle ``thread_key``'s task row (``/rename <title>``).

        The title is display state — ``/tasks`` listings, the client plane's thread
        pickers, restart pings — so a DB write is the whole job; nothing live needs
        repointing.
        """
        async with self._session_factory() as session:
            db = await get_task(
                session, platform=self._platform, thread_key=thread_key
            )
            if db is None:
                return NOTHING_TO_RENAME
            await set_title(session, db, title)
        return RENAME_CONFIRM.format(title=title)

    async def active_tasks(self) -> list[Task]:
        """Non-terminal tasks, for the ``/tasks`` listing."""
        async with self._session_factory() as session:
            return await list_active(session, platform=self._platform)

    async def composed_skills(self) -> list[str]:
        """The owner session's composed skill set: curated + chief-authored names.

        Mirrors ``_wire_owner_session``'s ``skills_on`` gate exactly (#138) — no
        plugin-manifest re-parse needed, since the vendored names already ARE
        ``self._default_skills``.
        """
        skills_on = self._skills_enabled and self._skills_plugin_path is not None
        if not skills_on:
            return []
        names = list(self._default_skills)
        if self._chief_skills_dir is not None:
            names += chief_skill_names(self._chief_skills_dir)
        return names

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
        classify_text: str | None = None,
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
                session,
                platform=self._platform,
                thread_key=thread_key,
                tier=tier,
                surface=surface.value,
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
        # Guests run on the guest model (Sonnet, never Opus, never routed onto a BYOK
        # class — #82); the owner on theirs — swapped for the cheaper budget model
        # while the cycle is downgraded (M9), reopened on Opus if the thread was
        # escalated (Task.model, M11), or on a routed category's target model + BYOK
        # provider (#79).
        if tier == "owner":
            target = await self._resolve_owner_target(
                thread_key=thread_key,
                persisted=persisted_model,
                surface=surface,
                classify_text=classify_text,
            )
        else:
            target = self._resolve_guest_target()
        model, provider = target.model, target.provider
        rt = _RunningTask(
            thread_key=thread_key,
            db_id=db_id,
            session=self._session_factory_sdk(
                model=model, resume=resume, provider=provider, **gate_kwargs
            ),
            queue=asyncio.Queue(),
            tier=tier,
            is_casual=is_casual,
            surface=surface,
            model=model,
            provider=provider,
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
        allowed = list(MEMORY_TOOLS)
        if workspace_on:
            # Write/Edit join the allow-list unconfined (no file_path check in
            # classify()) — containment is the approval blacklist, untrusted-content
            # screening, and the audit log.
            allowed += list(WORKSPACE_TOOLS)
        for svc in services:
            # Reads only — writes stay off the allow-list, but under owner default-allow
            # that alone no longer cards them; it's blacklist_tools (config.py seeds
            # every service's write_tools by default) that routes writes to approval.
            allowed += list(svc.read_tools)
        if admin is not None:
            # Owner-initiated, reversible → pre-approved (no card) to block/mute guests.
            allowed.append(admin.tool_name)
        imessage_admin = self._imessage_admin_service
        if imessage_admin is not None:
            # Owner-initiated, reversible whitelist edits (#156) — pre-approved
            # (no card), the manage_guest posture.
            allowed += list(imessage_admin.tool_names)
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
            web_enabled=self._web_service is not None,
            # The healthy Apple app areas (#155) — the doctor is plumbing, not a
            # capability the persona should advertise.
            apple_capabilities=tuple(
                svc.capability
                for svc in self._apple_services
                if svc.capability != "doctor"
            ),
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
            # Skills flow to the Copilot backend ONLY through ``skill_directories`` (#88
            # / #98). ``plugins`` + ``skills`` are the legacy claude-agent-sdk
            # plugin-manifest shape — kept as accepted-but-ignored kwargs
            # (:class:`~chief.core.backend.CopilotBackend` drops them by design), not a
            # second live path.
            assert self._skills_plugin_path is not None  # narrowed by skills_on
            gate_kwargs["plugins"] = [
                {"type": "local", "path": self._skills_plugin_path}
            ]
            gate_kwargs["skills"] = list(self._default_skills)
            # The live path (#87): the curated set as absolute skill directories.
            gate_kwargs["skill_directories"] = skill_directories_for(
                self._skills_plugin_path, self._default_skills
            )
        # chief-authored skills (#106, part of #103): gated on the same composed
        # ``skills_on`` as the vendored source (#118), so chief can't switch its own
        # skills on (skills_enabled is a denied overlay key, #103) and an authored dir
        # never arrives without the curated vendored set beside it. Each leaf SKILL.md
        # dir joins that set in the same kwarg; guests never reach here.
        if skills_on and self._chief_skills_dir is not None:
            authored = chief_skill_directories(self._chief_skills_dir)
            if authored:
                gate_kwargs["skill_directories"] = (
                    gate_kwargs.get("skill_directories", []) + authored
                )
        if self._subagents_enabled and self._subagents_dir is not None:
            # Owner-only, category-routed (#87). The subagents_dir is the SOLE source
            # (#103): scaffold_default_subagents seeds chief's built-ins there on first
            # boot (empty dir only — a surviving file is an expressed preference, never
            # overwritten), then load_subagent_specs reads them fresh at every spawn.
            # Each spec's model is resolved now through the live routing table, so a
            # category renamed/removed later (#83) still resolves; routing off ⇒
            # model omitted, the subagent runs on the parent model. build_custom_agents
            # refuses a non-owner tier regardless, so guests never carry a subagent.
            scaffold_default_subagents(self._subagents_dir)
            specs = load_subagent_specs(self._subagents_dir)
            gate_kwargs["custom_agents"] = build_custom_agents(
                specs, routing=self._routing, tier="owner"
            )
        # Each Google container (docker/mcp-*) + the in-process shell server. Google
        # writes and the shell tool are absent from allowed_tools, so every call routes
        # through can_use_tool → classify(); under owner default-allow that ALLOWs a
        # Google write too unless it's on blacklist_tools (config.py seeds every
        # service's write_tools there by default) — being off allowed_tools alone no
        # longer implies a card. Google deferred ops are blocked. Google reads are
        # pre-approved above + ALLOWed by the gate's extra_read_only.
        mcp_servers: dict[str, Any] = {
            svc.server_name: svc.server_config() for svc in services
        }
        if shell_on:
            shell = self._shell_service
            assert shell is not None  # narrowed by shell_on
            # Built per task: the bash closure addresses THIS task's own shell (keyed by
            # thread_key), since an in-process MCP handler gets no caller context. Kept
            # out of allowed_tools → every call routes through can_use_tool → classify.
            # Under owner default-allow that ALLOWs the call unless the command string
            # trips blacklist_shell_patterns (mcp__chief_shell__bash is one of
            # gate.policy.COMMAND_TOOLS, so its "command" input is checked against the
            # blacklist) — being off allowed_tools alone no longer implies a card.
            mcp_servers[shell.server_name] = shell.server_config(session_key=thread_key)
        if self._web_service is not None:
            # Owner-only web fetch/search (#81). Kept OFF allowed_tools so every call
            # routes through can_use_tool → classify: search ALLOWs under owner
            # default-allow, while fetch trips its blacklist_tools entry (config.py)
            # and raises an approval card. Its SSRF guard is the real boundary; the
            # card is defense in depth. The one server reaches both backends.
            web = self._web_service
            mcp_servers[web.server_name] = web.server_config()
        for apple in self._apple_services:
            # Apple ecosystem tools (#155), owner-only. Kept OFF allowed_tools so
            # every call routes through can_use_tool → classify: reads and
            # owner-local creates ALLOW under owner default-allow, while
            # run_shortcut and the Apple Calendar create_event trip their
            # blacklist_tools entries (config.py) and raise an approval card.
            mcp_servers[apple.server_name] = apple.server_config()
        if self._routing_admin_service is not None:
            # Self-config routing edits (#83), owner-only. Kept OFF allowed_tools so
            # every call routes through can_use_tool → classify: each mutating verb
            # trips its blacklist_tools entry (config.py) and raises an approval card,
            # while the read-only list_routing ALLOWs. The gate is the security boundary
            # for this self-modification surface. The one server reaches both backends.
            routing_admin = self._routing_admin_service
            mcp_servers[routing_admin.server_name] = routing_admin.server_config()
        if admin is not None:
            mcp_servers[admin.server_name] = admin.server_config()
        if imessage_admin is not None:
            mcp_servers[imessage_admin.server_name] = (
                imessage_admin.server_config()
            )
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
            # Register the server but keep its tool_names OFF the allow-list, so a mint
            # routes through can_use_tool → classify() rather than running unmediated.
            # Unlike the shell tool, schedule_bash's tool name isn't a COMMAND_TOOL and
            # isn't seeded into blacklist_tools by default (config.py only seeds each
            # Google service's write_tools there) — so today a mint currently ALLOWs
            # with no card under owner default-allow unless the owner also configures
            # blacklist_tools to include it. Being off allowed_tools routes the call
            # through the gate; it does not by itself raise a card.
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
                # Guest text is untrusted: a flagged message is still delivered, but
                # annotated so it reads as data, not instructions (host-native seam).
                await self._io.send(front_desk, await self._screen_relay(text))

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

    async def _screen_relay(self, text: str) -> str:
        """Screen a guest-relayed message; prepend the warning when flagged.

        Never drops or blocks the message — the owner should still see it — and never
        lets a screener error break the relay (fail-safe: deliver unannotated).
        """
        if self._screener is None:
            return text
        try:
            flagged = await self._screener(text)
        except Exception:
            logger.warning("relay screening failed; delivering as-is", exc_info=True)
            return text
        return prefix_flagged(text) if flagged else text

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
        # ALLOW with no card, so add them here for documentation intent. NOTE:
        # extra_read_only is genuinely inert for the owner tier — gate.classify()'s
        # owner branch never reads it (only the guest branch's `is_read_only(...) or
        # tool_name in extra_read_only` check does); an owner call already ALLOWs by
        # default unless NEVER-listed or blacklisted, with or without this set. Kept in
        # case the owner posture ever reverts toward default-ask. The separate gated
        # schedule_bash tools (self._schedule_bash_service, wired above under
        # mcp_servers) are deliberately left out of this set, but — for the same
        # reason — that omission has no effect on the owner tier either; see the
        # comment where bash_schedule is registered for what actually gates it.
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
        # The approval blacklist drives the owner's default-allow posture; a guest
        # session carries none and stays on the default-ask path (tier-split in
        # gate.classify — guests never gain the owner's open posture).
        blacklist = self._blacklist if tier == "owner" else None
        hook = build_pretool_hook(
            thread_key=thread_key,
            tier=tier,
            policy=self._policy,
            audit=self._audit,
            blacklist=blacklist,
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
            blacklist=blacklist,
            extra_read_only=extra_read_only,
        )
        hooks: dict[HookEvent, list[HookMatcher]] = {
            "PreToolUse": [HookMatcher(hooks=[hook])]
        }
        post_hooks: list[HookMatcher] = []
        # Screenshot delivery: owner sessions with a configured screenshots dir get
        # a PostToolUse hook that reads the saved file and delivers it via send_file.
        # Guest sessions never have browser tools — owner-only by construction.
        if tier == "owner" and self._screenshots_dir is not None:
            screenshot_hook = build_screenshot_hook(
                thread_key=thread_key,
                io=self._io,
                screenshots_dir=self._screenshots_dir,
            )
            post_hooks.append(HookMatcher(hooks=[screenshot_hook]))
        # Untrusted-content screening (host-native): owner web/browser results get an
        # injection screen; a hit is annotated (or blocked, per config). Guests have no
        # web tools, so their sessions skip it.
        if tier == "owner" and self._screener is not None and self._screening_tools:
            screening_hook = build_screening_hook(
                tools=self._screening_tools,
                screener=self._screener,
                block=self._screening_block,
            )
            post_hooks.append(HookMatcher(hooks=[screening_hook]))
        if post_hooks:
            hooks["PostToolUse"] = post_hooks
        return can_use_tool, hooks

    async def _submit(self, task: _RunningTask, turn: Turn) -> None:
        self._cancel_idle(task)
        if task.generating:
            if (
                await self._stop_intent(
                    turn.text,
                    model=self._classifier_model,
                    api_key=self._classifier_api_key,
                )
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

    # ---- model routing (#79) --------------------------------------------

    def _resolve_guest_target(self) -> ResolvedTarget:
        """The model + BYOK provider a guest session opens on — always Copilot quota.

        Guest isolation invariant (#82, part of #72): a guest turn must never resolve
        onto an ``openrouter`` target — nor onto any paid class added later — no matter
        what the routing table currently maps every category to, including after #83's
        runtime category add/remove/rename edits. This function is the guard: it
        never reads ``self._routing`` or any category at all, so there is no
        category-name allow/deny-list here for a rename to slip past. The one fact
        that matters is **target class** — this always returns the
        :data:`~chief.core.routing.TARGET_CLASS_COPILOT` class (``provider=None``),
        and the assertion below fails loudly if a future edit ever wires a BYOK
        provider in here by mistake, instead of silently leaking a guest onto paid
        quota.

        This is ``_ensure_task``'s guest branch — the *only* place a guest
        ``_RunningTask``'s model/provider are ever set. Every later re-target
        (``/route`` respawn, ``/opus``/``/sonnet``, budget downgrade) is gated on
        ``tier == "owner"`` or only reachable from an owner-only entry point, so a
        guest session's target is decided here once and never touched again.
        """
        target = ResolvedTarget(self._guest_model or self._owner_model, None)
        assert target.provider is None, (
            "guest target must stay on Copilot quota "
            f"({TARGET_CLASS_COPILOT}) — never openrouter or another BYOK class"
        )
        return target

    async def _resolve_owner_target(
        self,
        *,
        thread_key: str,
        persisted: str | None,
        surface: Surface,
        classify_text: str | None,
    ) -> ResolvedTarget:
        """The model + BYOK provider a new owner session opens on (#79).

        Composes with — never clobbers — the M11/#84 precedence:

        1. A persisted Opus escalation (``Task.model``) wins outright: the explicit
           owner choice overrides routing, on plain Copilot quota (no BYOK provider).
        2. An active budget downgrade (:meth:`_budget_downgraded` — the OpenRouter
           dollar budget exhausted on its own, or the owner tapped Downgrade on the
           premium-exhaustion card): re-target onto the cheaper Copilot class —
           ``budget_downgrade_model`` (``auto``), no provider. Sits *above* routing, so
           it wins over any route, including an openrouter one (#84, #97).
        3. Category routing (when a routing table is wired): the task's category
           (``/route`` override → per-surface default → classify → fallback) → the
           category's ``{target_class, model}`` → ``(model, provider)``.
        4. Otherwise the configured owner model (routing off), no provider.
        """
        if persisted == self._owner_model_opus:
            return ResolvedTarget(self._owner_model_opus)
        if self._budget_downgrade_model is not None and await self._budget_downgraded():
            return ResolvedTarget(self._budget_downgrade_model)
        if self._routing is not None:
            target = await self._resolve_route(thread_key, surface, classify_text)
            if target is not None:
                provider = provider_for_target(
                    target, openrouter_provider=self._openrouter_provider
                )
                return ResolvedTarget(target.model, provider)
        return ResolvedTarget(self._owner_model)

    async def _resolve_route(
        self, thread_key: str, surface: Surface, classify_text: str | None
    ) -> RoutingTarget | None:
        """The routing target for this task, or ``None`` if the table has no target."""
        assert self._routing is not None  # guarded by the caller
        category = await self._task_category(thread_key, surface, classify_text)
        return self._routing.resolve(category)

    async def _task_category(
        self, thread_key: str, surface: Surface, classify_text: str | None
    ) -> str:
        """Pick this task's job category at spawn (#79).

        Precedence: a persisted ``/route`` override wins; else a configured per-surface
        default pins the category *without* classifying; else the spawning message is
        auto-classified on the fixed cheap classifier model; else the general fallback.
        """
        assert self._routing is not None  # guarded by the caller
        override = await self._route_category_override(thread_key)
        if override is not None:
            return override
        surface_default = self._routing_surface_defaults.get(surface.value)
        if surface_default is not None:
            return surface_default
        if classify_text is not None:
            return await self._classify_category(
                classify_text,
                model=self._classifier_model,
                categories=self._routing.categories(),
                default=self._default_category,
                descriptions=self._routing.descriptions(),
                api_key=self._classifier_api_key,
            )
        return self._default_category

    async def _route_category_override(self, thread_key: str) -> str | None:
        """This thread's persisted ``/route`` category, or ``None``."""
        async with self._session_factory() as session:
            db = await get_task(
                session, platform=self._platform, thread_key=thread_key
            )
            return db.route_category if db is not None else None

    async def route(self, thread_key: str, category: str) -> str:
        """Override this owner thread's routing category (``/route <category>``, #79).

        Persists the per-task category (survives restart) and, when a session is live,
        **respawns** it onto the category's target — :meth:`SessionProto.set_model`
        cannot change a session's provider, so a target-class switch (e.g. copilot →
        openrouter) has to rebuild the session (resume-preserving) rather than re-point
        a live one. Rejects a category with no row (the table is the source of truth).
        """
        if self._routing is None:
            return ROUTING_DISABLED
        if not self._routing.has(category):
            known = ", ".join(self._routing.categories()) or "(none)"
            return UNKNOWN_CATEGORY.format(category=category, known=known)
        async with self._session_factory() as session:
            db = await get_or_create_task(
                session, platform=self._platform, thread_key=thread_key, tier="owner"
            )
            await set_route_category(session, db, category)
        task = self._tasks.get(thread_key)
        if task is not None:
            await self._respawn_for_route(task, category)
        target = self._routing.resolve(category)
        assert target is not None  # has(category) guaranteed a row above
        return ROUTE_CONFIRM.format(
            category=category,
            target_class=target.target_class,
            model=target.model,
        )

    async def _respawn_for_route(
        self, task: _RunningTask, category: str
    ) -> None:
        """Rebuild a live session on the routed category's target, resume-preserving."""
        assert self._routing is not None
        target = self._routing.resolve(category)
        assert target is not None
        provider = provider_for_target(
            target, openrouter_provider=self._openrouter_provider
        )
        await self._respawn_session(task, ResolvedTarget(target.model, provider))

    async def _respawn_session(
        self, task: _RunningTask, target: ResolvedTarget
    ) -> None:
        """Tear a live session down and reconnect it on ``target`` (resume-preserving).

        A provider can't change on a live session, so any switch that crosses the
        provider class has to rebuild rather than :meth:`SessionProto.set_model` — a
        ``/route`` target-class change (#79), or an ``/opus`` / ``/sonnet`` that moves a
        routed openrouter thread on or off plain Copilot quota (#94). Carries the resume
        id so the conversation's context follows, and the thread's active Google account
        so its calendar MCP config survives (#91) — mirrors the reseed/branch rebuild
        paths (issue #46).
        """
        resume = task.session.session_id
        if task.generating:
            await task.session.interrupt()
        await task.session.aclose()
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
            surface=task.surface,
            active_account_label=active_account_label,
        )
        task.session = self._session_factory_sdk(
            model=target.model, resume=resume, provider=target.provider, **gate_kwargs
        )
        task.model = target.model
        task.provider = target.provider

    async def _switch_live_session(
        self, task: _RunningTask, target: ResolvedTarget
    ) -> None:
        """Move a live session onto ``target`` the cheapest safe way.

        A same-provider model change is a live :meth:`SessionProto.set_model`; a switch
        that crosses the provider class (copilot ↔ openrouter) can't be done on a live
        session, so it goes through the resume-preserving respawn
        (:meth:`_respawn_session`). Shared by ``/route`` (via its own resolve),
        ``/opus``, and ``/sonnet`` (#94).
        """
        if target.provider != task.provider:
            await self._respawn_session(task, target)
        else:
            await task.session.set_model(target.model)
            task.model = target.model

    async def _budget_admits(self) -> bool:
        """False when the quota currency is paused at budget — the turn must be skipped
        without spending. Reminds the owner once per pause episode that turns are gated.

        Only the premium-request currency pauses (#84); an exhausted OpenRouter dollar
        budget *downgrades* (turns keep running on Copilot), so it never gates here.
        """
        if self._budget is None:
            return True
        if await self._budget.mode(usage.PREMIUM_REQUESTS) != usage.MODE_PAUSED:
            self._paused_ack_sent = False
            return True
        if not self._paused_ack_sent and self._owner_inbox is not None:
            self._paused_ack_sent = True
            await self._io.send(self._owner_inbox, PAUSED_BUDGET_ACK)
        return False

    async def _budget_downgraded(self) -> bool:
        """True when a budgeted currency is ``downgraded`` this cycle (#84, #97).

        Owner resolution (:meth:`_resolve_owner_target`) and live sessions
        (:meth:`downgrade_live_sessions`) both re-target onto the cheaper Copilot
        ``auto`` class when either downgrade trigger is active, so new and live owner
        sessions always agree on the target:

        * the **OpenRouter dollar** budget exhausting on its own (auto, via the gate's
          ``ACTION_DOWNGRADE`` effect, #84); or
        * the owner tapping **Downgrade** on the premium-exhaustion card, which flips
          the **premium-request currency** out of ``paused`` into ``downgraded`` — so
          :meth:`_budget_admits` stops gating and turns resume on the cheaper model
          (#97). Continue/Overflow move it to their own modes.

        Reads only the persisted per-currency mode (no side effects).
        """
        if self._budget is None:
            return False
        if await self._budget.mode(usage.OPENROUTER_DOLLARS) == usage.MODE_DOWNGRADED:
            return True
        quota_mode = await self._budget.mode(usage.PREMIUM_REQUESTS)
        return quota_mode == usage.MODE_DOWNGRADED

    def _turn_currency(self, task: _RunningTask) -> tuple[str, float]:
        """The native currency + amount this turn spent (#84).

        An openrouter (BYOK provider) turn spends metered dollars; a plain-quota turn
        spends Copilot premium requests (summed from the raw snapshot #80).
        """
        if task.provider is not None:
            return usage.OPENROUTER_DOLLARS, task.session.last_cost_usd
        return usage.PREMIUM_REQUESTS, premium_request_total(
            task.session.last_premium_requests
        )

    async def _record_spend(self, task: _RunningTask) -> None:
        """Meter this turn in its native currency; a hard rate-limit rejection is
        treated like exhaustion of that currency (pause the quota, or downgrade)."""
        # The actually-served model is observability only (#79): on Copilot ``auto`` and
        # on OpenRouter alike the real model is read from the turn's event, not assumed
        # from what was requested (set_model is untrusted on Copilot quota, spike #74).
        served = task.session.last_served_model
        if served is not None:
            logger.info(
                "turn served model",
                extra={"thread_key": task.thread_key, "served_model": served},
            )
        if self._budget is None:
            return
        currency, amount = self._turn_currency(task)
        await self._apply_budget_effect(await self._budget.record(currency, amount))
        if task.session.last_rate_limit_status == "rejected":
            await self._apply_budget_effect(
                await self._budget.note_rate_limited(currency)
            )

    async def _apply_budget_effect(self, effect: str | None) -> None:
        """Complete a budget action the gate signalled: a downgrade needs the engine to
        switch live sessions onto the cheaper class (the gate owns no live session)."""
        if effect == EFFECT_DOWNGRADE:
            await self.downgrade_live_sessions()

    async def downgrade_live_sessions(self) -> None:
        """Switch every live owner session onto the cheaper Copilot class (#84).

        Invoked when the OpenRouter dollar budget exhausts (auto, via the gate's effect)
        or the owner taps **Downgrade** on the budget card; new sessions already pick it
        up via :meth:`_resolve_owner_target`. No-op when no downgrade model is
        configured (budget disabled). The target is ``{budget_downgrade_model, provider
        None}`` — Copilot ``auto``.

        A routed **openrouter** session crosses the provider class to get there, which a
        live ``set_model`` can't do, so this uses :meth:`_switch_live_session`: a
        same-class copilot session takes the cheap live ``set_model``, an openrouter one
        the resume-preserving respawn (#94, carrying the thread's Google account #91).
        An Opus-pinned thread is **skipped**: an explicit escalation overrides a budget
        downgrade — the owner chose to spend faster and was warned (matching the reopen
        precedence in :meth:`_resolve_owner_target`; ``/sonnet`` drops it).
        """
        if self._budget_downgrade_model is None:
            return
        target = ResolvedTarget(self._budget_downgrade_model)  # Copilot auto
        for task in list(self._tasks.values()):
            if task.tier != "owner" or task.model == self._owner_model_opus:
                continue
            await self._switch_live_session(task, target)

    # ---- Opus escalation (M11) ------------------------------------------

    async def escalate(self, thread_key: str) -> str:
        """Switch the owner thread to Opus now and persist it (``/opus``, M11).

        Persists ``Task.model`` so the thread reopens on Opus after a restart, and — if
        a session is live — switches it mid-thread so the next turn (including the one
        that triggered an auto-detect card) runs on Opus. Explicit escalation overrides
        an active budget downgrade; the reply warns that Opus burns the credit faster.

        Escalation wins outright onto plain Copilot quota (no BYOK provider) per the
        resolution order in :meth:`_resolve_owner_target`, so the live switch targets
        ``{opus, provider=None}``. For a routed openrouter thread that crosses the
        provider class, so :meth:`_switch_live_session` respawns rather than a bare
        ``set_model`` that would strand the turn on opus-through-OpenRouter (#94); it
        also clears the tracked ``task.provider`` so a later reseed/branch (or the
        downgrade skip-check) can't misread it as still-openrouter (#92).
        """
        async with self._session_factory() as session:
            db = await get_or_create_task(
                session, platform=self._platform, thread_key=thread_key, tier="owner"
            )
            await set_task_model(session, db, self._owner_model_opus)
        task = self._tasks.get(thread_key)
        if task is not None:
            opus = ResolvedTarget(self._owner_model_opus)  # opus, plain Copilot quota
            await self._switch_live_session(task, opus)
            task.auto_escalate_suppressed = False
        note = f" {OPUS_BUDGET_NOTE}" if await self._budget_downgraded() else ""
        return f"{OPUS_CONFIRM}{note}"

    async def revert(self, thread_key: str) -> str:
        """Clear an Opus escalation; drop back to the thread's resolved target
        (``/sonnet``, M11).

        Clears the persisted ``Task.model`` and, if a session is live, restores what the
        thread otherwise resolves to via :meth:`_resolve_owner_target` — routing-aware
        (#94), so an openrouter-routed thread returns to its category's
        ``{model, provider}`` rather than plain owner-model Copilot quota. Precedence
        after the escalation clears is budget downgrade > routing > owner model. Because
        dropping the escalation can cross the provider class (Opus runs on plain Copilot
        quota; the restored target may be openrouter), :meth:`_switch_live_session`
        respawns when the provider changes — a live ``set_model`` can't move it.
        """
        async with self._session_factory() as session:
            db = await get_task(
                session, platform=self._platform, thread_key=thread_key
            )
            if db is not None:
                await set_task_model(session, db, None)
        task = self._tasks.get(thread_key)
        if task is not None:
            target = await self._resolve_owner_target(
                thread_key=thread_key,
                persisted=None,
                surface=task.surface,
                classify_text=None,
            )
            await self._switch_live_session(task, target)
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
        if not await self._is_complex(
            text, model=self._classifier_model, api_key=self._classifier_api_key
        ):
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

    def _streams_per_block(self, task: _RunningTask) -> bool:
        """True when the reply should be streamed one text block at a time.

        Owner home/DM (issue #64) and owner GROUP (issue #67) all stream per-block.
        Guest turns accumulate into a single joined message (unchanged behaviour).
        """
        return task.tier == "owner"

    async def _run_turn(self, task: _RunningTask, turn: Turn) -> None:
        if not await self._budget_admits():
            return  # paused at budget — skip without spending (owner already nudged)
        await self._maybe_auto_escalate(task, turn.text)
        task.transcript.append(("owner", turn.text))
        try:
            async with self._semaphore:
                task.generating = True
                await self._set_status(task, RUNNING)
                # Owner turns (home, DM, group) stream each text block immediately as
                # it arrives (per-block streaming, issues #64/#67).  Guest turns
                # accumulate all blocks into a single joined message (unchanged).
                per_block = self._streams_per_block(task)
                block_parts: list[str] = []
                per_block_sent = 0  # non-empty blocks delivered in per-block mode
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
                                # Guard against empty/whitespace-only blocks that the
                                # SDK emits as separators around tool calls (#69).
                                # Both Telegram and Discord reject empty content.
                                stripped = event.text.strip()
                                if stripped:
                                    await self._emit_final(task, stripped)
                                    per_block_sent += 1
                            else:
                                block_parts.append(event.text)
                        else:
                            await self._io.send(task.thread_key, f"· {event.text}")
                if task.cancelled:
                    return
                if per_block:
                    # Empty per-block turn on GROUP: post NO_REPLY so the owner gets an
                    # acknowledgement in the shared room. Home/DM stay silent.
                    if per_block_sent == 0 and task.surface is Surface.GROUP:
                        await self._emit_final(task, NO_REPLY)
                else:
                    # Accumulated path (guest): join and emit as one message,
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
                # Commit the harness dir too (#110): a separate root + lock from the
                # memory versioner above, so a subagent/skill write this turn made
                # lands as its own revertible commit. Also skips empty commits.
                await self._harness_versioner.commit("chief: harness auto-save")
                # Only a clean turn re-arms the idle→archive timer.
                self._arm_idle(task)
        except TimeoutError:
            logger.warning("task turn timed out", extra={"thread_key": task.thread_key})
            await self._set_status(task, FAILED)
            await self._io.send(task.thread_key, TURN_TIMEOUT_NOTE)
            await self._reset_session(task)
        except Exception:
            logger.exception("task turn failed", extra={"thread_key": task.thread_key})
            await self._set_status(task, FAILED)
            await self._io.send(task.thread_key, "⚠️ that task hit an error.")
        finally:
            task.generating = False

    async def _reset_session(self, task: _RunningTask) -> None:
        """Best-effort teardown of a wedged session so the next turn reconnects fresh.

        The watchdog fired because the turn never terminated, so ``interrupt`` may also
        hang on the wedged control stream; it is time-boxed and guarded. The close then
        gets a fresh CLI next turn — but a time-boxed ``aclose`` cancelled mid-teardown
        *leaks* the Copilot CLI subprocess (#101), so it goes through the shared
        :func:`close_wedged_session`, which reaps the orphan on expiry. This teardown
        runs on the consumer loop, so a hung step here would re-freeze the task we just
        rescued — hence the bounds.
        """
        try:
            async with asyncio.timeout(_RESET_TIMEOUT):
                await task.session.interrupt()
        except Exception:
            logger.debug("interrupt during turn-timeout reset failed", exc_info=True)
        await close_wedged_session(task.session, timeout=_RESET_TIMEOUT)

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

        The reseeded session opens on ``task.model``/``task.provider`` (#92) — the live
        task's fields already carry the precedence-resolved target (Opus escalation >
        budget downgrade > routing > owner model), so the priming turn (and the
        reseeded ``sdk_session_id`` it mints) lands on the same target the task was
        actually running on, not a hardcoded owner-model/Copilot-quota fallback.
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
            model=task.model, resume=None, provider=task.provider, **gate_kwargs
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

        The promoted task opens on the casual channel's resolved target (#92), not a
        hardcoded owner model — see :meth:`_resolve_branch_target`.
        """
        casual_resume = await self._casual_session_id(thread_key)
        target = await self._resolve_branch_target(thread_key)
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
                model=target.model,
                resume=casual_resume,
                # Fork only when there's a session to fork; an empty casual (no turn
                # yet) has no context to carry, so the new thread just starts fresh.
                fork_session=casual_resume is not None,
                provider=target.provider,
                **gate_kwargs,
            ),
            queue=asyncio.Queue(),
            tier="owner",
            model=target.model,
            provider=target.provider,
        )
        self._tasks[new_key] = rt
        return new_key

    async def _resolve_branch_target(self, thread_key: str) -> ResolvedTarget:
        """The model + provider a branched-off task opens on (#92).

        A live casual task's ``.model``/``.provider`` are already precedence-resolved
        (Opus escalation > budget downgrade > routing > owner model) as of its last
        (re)spawn, so branching reuses them directly rather than re-deriving — that's
        also the only way to carry forward an auto-classified category, since only an
        explicit ``/route`` persists ``route_category``; a fresh classify has no turn
        text to classify (``branch`` takes just a title).

        Falls back to :meth:`_resolve_owner_target` — keyed on the casual
        ``thread_key`` so its own persisted state (an Opus escalation, an explicit
        ``/route`` override) is consulted — when the casual task isn't live (e.g. it
        was already compacted away, or never ran).
        """
        live = self._tasks.get(thread_key)
        if live is not None:
            return ResolvedTarget(live.model, live.provider)
        async with self._session_factory() as session:
            db = await get_task(
                session, platform=self._platform, thread_key=thread_key
            )
            persisted = db.model if db is not None else None
        return await self._resolve_owner_target(
            thread_key=thread_key,
            persisted=persisted,
            surface=Surface.DM,
            classify_text=None,
        )

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
