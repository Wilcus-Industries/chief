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
  and its thread archived; the next message reopens it (resume). (Memory distillation is
  a separate ~10-min trigger owned by M4.)
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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from claude_agent_sdk import CanUseTool, HookMatcher
from claude_agent_sdk.types import HookEvent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..gate.approvals import ApprovalManager
from ..gate.gate import (
    BUILTIN_SHELL_TOOLS,
    FILE_OP_TOOLS,
    WRITE_OP_TOOLS,
    build_can_use_tool,
    build_pretool_hook,
)
from ..gate.policy import PolicyStore
from ..memory.distill import distill as _distill_default
from ..memory.store import OWNER_NAMESPACE, FactDraft, MemoryStore
from ..obs.audit import AuditLog
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
    set_session_id,
    set_status,
)
from ..tools.google import GoogleService
from ..tools.guest import GuestAdminService, GuestService
from ..tools.shell import ShellService
from . import classify
from .personas import build_system_prompt
from .session import Final, TaskSession, TurnEvent

logger = logging.getLogger("chief.core.tasks")

WORKING_ACK = "working on it…"
#: Read-only file tools chief gets at M4, confined to the memory dir by the gate.
MEMORY_TOOLS = sorted(FILE_OP_TOOLS)
#: Write file tools the owner gets at M7 when the workspace is enabled — added to
#: ``allowed_tools`` but confined to the workspace by the gate (writes elsewhere DENY).
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
#: A distiller: turn a transcript + the current index into candidate facts.
DistillFn = Callable[..., Awaitable[list[FactDraft]]]
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


class TaskIO(Protocol):
    """How the engine talks back to a platform (implemented by the adapter)."""

    async def send(self, thread_key: str, text: str) -> None: ...
    async def create_thread(self, *, like_thread_key: str, title: str) -> str: ...
    async def archive_thread(self, thread_key: str) -> None: ...


class SessionProto(Protocol):
    """The slice of :class:`TaskSession` the engine drives (structural)."""

    session_id: str | None

    def run_turn(self, text: str) -> AsyncIterator[TurnEvent]: ...
    async def interrupt(self) -> None: ...
    async def aclose(self) -> None: ...


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
    )


def _title(text: str) -> str:
    first = next((line for line in text.strip().splitlines() if line.strip()), "task")
    return first.strip()[:60] or "task"


@dataclass
class _RunningTask:
    thread_key: str
    db_id: int
    session: SessionProto
    queue: "asyncio.Queue[str]"
    tier: str
    is_casual: bool = False
    generating: bool = False
    cancelled: bool = False
    consumer: "asyncio.Task[None] | None" = None
    idle_handle: "asyncio.Task[None] | None" = None
    distill_handle: "asyncio.Task[None] | None" = None
    transcript: list[tuple[str, str]] = field(default_factory=list)


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
        idle_archive_seconds: float = 3600.0,
        compaction_idle_seconds: float = 3600.0,
        session_factory_sdk: SessionFactory = _default_session,
        stop_intent: Classifier = classify.stop_intent,
        warrants_task: Classifier = classify.warrants_task,
        policy: PolicyStore | None = None,
        approvals: ApprovalManager | None = None,
        audit: AuditLog | None = None,
        front_desk_thread_key: str | None = None,
        memory: MemoryStore | None = None,
        memory_dir: str | None = None,
        owner_name: str = "the owner",
        distill_idle_seconds: float = 1200.0,
        distill_model: str = "claude-sonnet-4-6",
        distill: DistillFn = _distill_default,
        google_services: Sequence[GoogleService] = (),
        owner_tz: str = "UTC",
        shell_service: ShellService | None = None,
        workspace_dir: str | None = None,
        guest_model: str | None = None,
        guest_calendar_service: GoogleService | None = None,
        guest_admin_service: GuestAdminService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._io = io
        self._owner_model = owner_model
        self._classifier_model = classifier_model
        self._platform = platform
        self._grace_seconds = grace_seconds
        self._idle_archive_seconds = idle_archive_seconds
        self._compaction_idle_seconds = compaction_idle_seconds
        self._session_factory_sdk = session_factory_sdk
        self._stop_intent = stop_intent
        self._warrants_task = warrants_task
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
        self._front_desk_thread_key = front_desk_thread_key
        self._memory = memory
        self._memory_dir = memory_dir
        self._owner_name = owner_name
        self._distill_idle_seconds = distill_idle_seconds
        self._distill_model = distill_model
        self._distill = distill
        self._google_services = tuple(google_services)
        self._owner_tz = owner_tz
        self._shell_service = shell_service
        self._workspace_dir = workspace_dir
        self._guest_model = guest_model
        self._guest_calendar_service = guest_calendar_service
        self._guest_admin_service = guest_admin_service
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: dict[str, _RunningTask] = {}

    # ---- inbound routing -------------------------------------------------

    async def dispatch(
        self, *, thread_key: str, text: str, is_general: bool = False
    ) -> None:
        """Route an owner message into its task, spawning a topic when warranted."""
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
        await self._submit(task, text)

    async def dispatch_guest(
        self, *, thread_key: str, text: str, from_label: str | None = None
    ) -> None:
        """Route a guest DM into its flat per-DM session (no topic spawn, guest model).

        Unlike :meth:`dispatch`, a guest never spawns a forum topic: a 1:1 DM is one
        flat session keyed by its ``thread_key``. ``from_label`` is the sender's display
        name, baked into the relay tool so the owner sees who left a message.
        """
        task = await self._ensure_task(
            thread_key, tier="guest", from_label=from_label
        )
        await self._submit(task, text)

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
            db_id, resume = db.id, db.sdk_session_id
        gate_kwargs = self._session_kwargs(
            thread_key=thread_key, tier=tier, db_id=db_id, from_label=from_label
        )
        # Guests run on the guest model (Sonnet, never Opus); the owner on theirs.
        model = (
            self._owner_model
            if tier == "owner"
            else (self._guest_model or self._owner_model)
        )
        rt = _RunningTask(
            thread_key=thread_key,
            db_id=db_id,
            session=self._session_factory_sdk(
                model=model, resume=resume, **gate_kwargs
            ),
            queue=asyncio.Queue(),
            tier=tier,
            is_casual=is_casual,
        )
        self._tasks[thread_key] = rt
        return rt

    def _session_kwargs(
        self, *, thread_key: str, tier: str, db_id: int, from_label: str | None = None
    ) -> dict[str, Any]:
        """Assemble the SDK session kwargs (gate + memory/tool scoping) for a thread.

        Shared by :meth:`_ensure_task`, casual reseed, and :meth:`branch` so a session
        is built the same way wherever it originates (no per-call drift). ``resume`` /
        ``fork_session`` are layered on by the caller — they vary per origin. The owner
        and guest tool surfaces diverge sharply (tier isolation by construction), so
        each is wired by its own helper; ``from_label`` is the guest's display name.
        """
        can_use_tool, hooks = self._build_gate(
            task_id=db_id, thread_key=thread_key, tier=tier
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
            self._wire_owner_session(gate_kwargs, thread_key, disallowed_tools)
        else:
            self._wire_guest_session(gate_kwargs, from_label)
        return gate_kwargs

    def _wire_owner_session(
        self, gate_kwargs: dict[str, Any], thread_key: str, disallowed_tools: list[str]
    ) -> None:
        """Wire the owner's full surface: memory + web + Google + shell + admin."""
        assert self._memory is not None
        services = self._google_services
        workspace_on = self._workspace_dir is not None
        shell_on = self._shell_service is not None
        admin = self._guest_admin_service
        allowed = list(MEMORY_TOOLS) + list(WEB_META_TOOLS)
        if workspace_on:
            # Write/Edit join the allow-list; the gate confines them to /workspace.
            allowed += list(WORKSPACE_TOOLS)
        for svc in services:
            # Reads only — writes stay off the allow-list so they reach approval.
            allowed += list(svc.read_tools)
        if admin is not None:
            # Owner-initiated, reversible → pre-approved (no card) to block/mute guests.
            allowed.append(admin.tool_name)
        gate_kwargs.update(
            system_prompt=build_system_prompt(
                tier="owner",
                memory=self._memory,
                owner_name=self._owner_name,
                google_services=frozenset(svc.name for svc in services),
                owner_tz=self._owner_tz,
                workspace_enabled=workspace_on,
                shell_enabled=shell_on,
                guest_admin_enabled=admin is not None,
            ),
            cwd=self._memory_dir,
            allowed_tools=allowed,
        )
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
            ),
            allowed_tools=allowed,
        )
        # Hard-deny the owner's built-in file/web/write tools at the SDK layer too — not
        # just absent from allowed_tools but refused outright (like the built-in shell),
        # so a guest can never read files or the web even if a call reaches the gate.
        gate_kwargs["disallowed_tools"] = gate_kwargs["disallowed_tools"] + GUEST_DENIED
        if mcp_servers:
            gate_kwargs["mcp_servers"] = mcp_servers

    def _build_gate(
        self, *, task_id: int, thread_key: str, tier: str
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
        # Owner work approves in-thread; a guest-originated approval routes to the Front
        # Desk. A guest with no Front Desk configured is a hard error — never silently
        # self-route a card back into the guest's own DM (config also guards this).
        if tier == "owner":
            route = thread_key
        elif self._front_desk_thread_key is not None:
            route = self._front_desk_thread_key
        else:
            raise RuntimeError(
                "guest approval has no Front Desk route — set front_desk_thread_key"
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
        return can_use_tool, hooks

    async def _submit(self, task: _RunningTask, text: str) -> None:
        self._cancel_idle(task)
        self._cancel_distill(task)
        if task.generating:
            if (
                await self._stop_intent(text, model=self._classifier_model)
                and task.generating
            ):
                await task.session.interrupt()
        elif task.consumer is None or task.consumer.done():
            task.consumer = asyncio.create_task(self._consume(task))
        task.queue.put_nowait(text)

    async def _consume(self, task: _RunningTask) -> None:
        while True:
            text = await task.queue.get()
            await self._run_turn(task, text)

    async def _run_turn(self, task: _RunningTask, text: str) -> None:
        ack = asyncio.create_task(self._ack_after_grace(task))
        task.transcript.append(("owner", text))
        try:
            async with self._semaphore:
                task.generating = True
                await self._set_status(task, RUNNING)
                final: Final | None = None
                async for event in task.session.run_turn(text):
                    if task.cancelled:
                        break  # interrupted — stop streaming its milestones
                    if isinstance(event, Final):
                        final = event
                    else:
                        await self._io.send(task.thread_key, f"· {event.text}")
                ack.cancel()
                if task.cancelled:
                    return
                if final is not None:
                    await self._io.send(task.thread_key, final.text)
                    task.transcript.append(("chief", final.text))
                if task.session.session_id:
                    await self._set_session_id(task, task.session.session_id)
                await self._set_status(task, OPEN)
                # Only a clean turn re-arms the idle→archive and distill timers.
                self._arm_idle(task)
                self._arm_distill(task)
        except Exception:
            logger.exception("task turn failed", extra={"thread_key": task.thread_key})
            await self._set_status(task, FAILED)
            await self._io.send(task.thread_key, "⚠️ that task hit an error.")
        finally:
            ack.cancel()
            task.generating = False

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
        # a real task thread archives and dies. Everything else (live session, resume,
        # steering, distill@20m) is shared.
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
        self._cancel_distill(task)
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
        self._cancel_distill(task)
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
        gate_kwargs = self._session_kwargs(
            thread_key=task.thread_key, tier=task.tier, db_id=task.db_id
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
        """Drive one turn on ``session`` silently (not surfaced); return its text."""
        final: Final | None = None
        async for event in session.run_turn(text):
            if isinstance(event, Final):
                final = event
        return final.text if final is not None else ""

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
        gate_kwargs = self._session_kwargs(
            thread_key=new_key, tier="owner", db_id=db_id
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

    # ---- distillation (auto-learning; mirrors the idle-archive trio) ------

    def _arm_distill(self, task: _RunningTask) -> None:
        if (
            self._memory is None
            or task.tier != "owner"
            or task.cancelled
            or self._tasks.get(task.thread_key) is not task
        ):
            return  # no memory, a guest (low-trust), or torn down/cancelled — don't arm
        self._cancel_distill(task)
        task.distill_handle = asyncio.create_task(self._distill_then_notify(task))

    def _cancel_distill(self, task: _RunningTask) -> None:
        if task.distill_handle is not None:
            task.distill_handle.cancel()
            task.distill_handle = None

    async def _distill_then_notify(self, task: _RunningTask) -> None:
        """After a quiet spell, distill the transcript into facts and save them.

        Unlike idle-archive this leaves the task OPEN: learning is a side effect, not an
        end of life. The distill window (~20m) is shorter than the archive one (~60m),
        so a task's chatter is captured before it is archived.
        """
        try:
            await asyncio.sleep(self._distill_idle_seconds)
        except asyncio.CancelledError:
            return
        if (
            self._memory is None
            or task.cancelled
            or self._tasks.get(task.thread_key) is not task
        ):
            return
        pending = list(task.transcript)  # snapshot; cleared only after a clean flush
        if not pending:
            return
        try:
            drafts = await self._distill(
                pending, self._memory.index(), model=self._distill_model
            )
            written = [
                await self._memory.write_fact(
                    namespace=OWNER_NAMESPACE,
                    slug=draft.slug,
                    title=draft.title,
                    body=draft.body,
                    provenance="inferred",
                    trust=draft.trust,
                    expires=draft.expires,
                )
                for draft in drafts
            ]
            # Drop the distilled turns now the writes have landed. A cancel (new turn
            # racing the timer) or an error before this leaves them — and any turns
            # appended meanwhile — intact for the next pass, instead of losing them.
            del task.transcript[: len(pending)]
            if not written:
                return
            lines = "\n".join(f"• {fact.title}" for fact in written)
            await self._io.send(task.thread_key, f"📝 Saved to memory:\n{lines}")
            if self._audit is not None:
                self._audit.log(
                    {
                        "event": "memory_write",
                        "thread_key": task.thread_key,
                        "count": len(written),
                    }
                )
        except Exception:
            logger.exception(
                "distillation failed", extra={"thread_key": task.thread_key}
            )

    async def _stop_task(self, task: _RunningTask) -> None:
        """Tear down: cancel the timers + consumer (awaited) and close the session."""
        task.cancelled = True
        self._cancel_idle(task)
        self._cancel_distill(task)
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
