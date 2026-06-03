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
- **Restart recovery.** :meth:`recover` pings the owner for tasks left mid-flight and
  never auto-resumes; a follow-up message resumes from the persisted ``sdk_session_id``.

The engine is platform-neutral: speaks only :class:`TaskIO` (provided by the adapter).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from claude_agent_sdk import CanUseTool, HookMatcher
from claude_agent_sdk.types import HookEvent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..gate.approvals import ApprovalManager
from ..gate.gate import build_can_use_tool, build_pretool_hook
from ..gate.policy import PolicyStore
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
from . import classify
from .session import Final, TaskSession, TurnEvent

logger = logging.getLogger("chief.core.tasks")

WORKING_ACK = "working on it…"


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
    can_use_tool: CanUseTool | None = None,
    hooks: dict[HookEvent, list[HookMatcher]] | None = None,
) -> SessionProto:
    return TaskSession(
        model=model, resume=resume, can_use_tool=can_use_tool, hooks=hooks
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
    generating: bool = False
    cancelled: bool = False
    consumer: "asyncio.Task[None] | None" = None
    idle_handle: "asyncio.Task[None] | None" = None


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
        session_factory_sdk: SessionFactory = _default_session,
        stop_intent: Classifier = classify.stop_intent,
        warrants_task: Classifier = classify.warrants_task,
        policy: PolicyStore | None = None,
        approvals: ApprovalManager | None = None,
        audit: AuditLog | None = None,
        front_desk_thread_key: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._io = io
        self._owner_model = owner_model
        self._classifier_model = classifier_model
        self._platform = platform
        self._grace_seconds = grace_seconds
        self._idle_archive_seconds = idle_archive_seconds
        self._session_factory_sdk = session_factory_sdk
        self._stop_intent = stop_intent
        self._warrants_task = warrants_task
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
        self._front_desk_thread_key = front_desk_thread_key
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: dict[str, _RunningTask] = {}

    # ---- inbound routing -------------------------------------------------

    async def dispatch(
        self, *, thread_key: str, text: str, is_general: bool = False
    ) -> None:
        """Route an owner message into its task, spawning a topic when warranted."""
        if is_general and await self._warrants_task(
            text, model=self._classifier_model
        ):
            new_key = await self._io.create_thread(
                like_thread_key=thread_key, title=_title(text)
            )
            await self._io.send(thread_key, "→ Tracking that in a new topic.")
            thread_key = new_key
        task = await self._ensure_task(thread_key)
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
        self, thread_key: str, *, tier: str = "owner"
    ) -> _RunningTask:
        existing = self._tasks.get(thread_key)
        if existing is not None:
            if not existing.cancelled:
                return existing
            # An idle-archive teardown is in flight; wait it out so the DB status
            # and topic settle (DONE + closed) before we reopen/resume the task.
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
        can_use_tool, hooks = self._build_gate(
            task_id=db_id, thread_key=thread_key, tier=tier
        )
        gate_kwargs: dict[str, Any] = {}
        if can_use_tool is not None or hooks is not None:
            gate_kwargs = {"can_use_tool": can_use_tool, "hooks": hooks}
        rt = _RunningTask(
            thread_key=thread_key,
            db_id=db_id,
            session=self._session_factory_sdk(
                model=self._owner_model, resume=resume, **gate_kwargs
            ),
            queue=asyncio.Queue(),
            tier=tier,
        )
        self._tasks[thread_key] = rt
        return rt

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
        # Owner work approves in-thread. A guest-originated approval must route to the
        # Front Desk; until M6 wires it (and the first guest-facing effectful tool), no
        # such call exists, so the ``or thread_key`` fallback is unreachable — M6 should
        # make a missing front_desk_thread_key a hard error rather than self-route.
        route = (
            thread_key
            if tier == "owner"
            else (self._front_desk_thread_key or thread_key)
        )
        hook = build_pretool_hook(
            thread_key=thread_key, tier=tier, policy=self._policy, audit=self._audit
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
        )
        hooks: dict[HookEvent, list[HookMatcher]] = {
            "PreToolUse": [HookMatcher(hooks=[hook])]
        }
        return can_use_tool, hooks

    async def _submit(self, task: _RunningTask, text: str) -> None:
        self._cancel_idle(task)
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
                if task.session.session_id:
                    await self._set_session_id(task, task.session.session_id)
                await self._set_status(task, OPEN)
                self._arm_idle(task)  # only a clean turn re-arms the idle→archive timer
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
        task.idle_handle = asyncio.create_task(self._idle_then_archive(task))

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

    async def _stop_task(self, task: _RunningTask) -> None:
        """Tear down: cancel the timer + consumer (awaited) and close the session."""
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
