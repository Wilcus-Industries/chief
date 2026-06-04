"""Entrypoint — wire settings, logging, db, the task engine, and the adapter, then run.

Run with ``python -m chief.app``. Everything runs on one asyncio loop
(:func:`asyncio.run`): schema init, the engine's per-task turns/timers, and the
adapter's long-poll all share it. Restart recovery runs once the connection is live
(``on_ready``), so it can ping the owner about tasks left mid-flight.
"""

import asyncio
import os

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram.ext import Application

from .adapters.telegram import TelegramAdapter, TelegramTaskIO
from .config import Settings
from .core.tasks import TaskManager
from .gate.approvals import ApprovalManager
from .gate.policy import PolicyStore
from .memory.markdown_backend import MarkdownMemory
from .memory.store import MemoryStore
from .memory.versioning import GitVersioner, NullVersioner, Versioner
from .obs.audit import AuditLog
from .obs.logging import configure_logging
from .persistence.db import create_engine, init_db, session_factory

DOCKER_SECRETS_DIR = "/run/secrets"


def load_settings() -> Settings:
    """Load settings, using Docker secrets when mounted and env otherwise.

    Disabling the secrets source when ``/run/secrets`` is absent avoids a noisy
    "directory does not exist" warning on local runs, where tokens come from env.
    """
    if os.path.isdir(DOCKER_SECRETS_DIR):
        return Settings(_secrets_dir=DOCKER_SECRETS_DIR)  # type: ignore[call-arg]
    return Settings()  # type: ignore[call-arg]


def build_memory(settings: Settings) -> MemoryStore:
    """Construct the markdown memory store + its versioner from settings."""
    versioner: Versioner = (
        GitVersioner(
            settings.memory_dir,
            author_name=settings.git_author_name,
            author_email=settings.git_author_email,
        )
        if settings.memory_git
        else NullVersioner()
    )
    return MarkdownMemory(
        settings.memory_dir, versioner=versioner, owner_name=settings.owner_name
    )


def build_components(
    settings: Settings,
    *,
    application: Application,  # type: ignore[type-arg]
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[TaskManager, TelegramAdapter, PolicyStore, ApprovalManager, MemoryStore]:
    """Wire the gate, memory, engine, and adapter against a built ``application``.

    The one ``TelegramTaskIO`` doubles as the engine's ``TaskIO`` and the approval
    ``ApprovalIO`` (it implements both), so cards post through the same bot. Policy
    seeding, memory scaffolding, and pending-approval re-arming are async and happen in
    :func:`serve`.
    """
    io = TelegramTaskIO(application.bot)
    audit = AuditLog(settings.audit_log_path)
    policy = PolicyStore(session_factory, audit=audit)
    approvals = ApprovalManager(
        session_factory=session_factory,
        io=io,
        policy=policy,
        audit=audit,
        timeout_seconds=settings.approval_timeout_seconds,
    )
    memory = build_memory(settings)
    manager = TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model=settings.owner_model_default,
        classifier_model=settings.classifier_model,
        concurrency=settings.concurrency,
        grace_seconds=settings.grace_seconds,
        idle_archive_seconds=settings.idle_archive_seconds,
        policy=policy,
        approvals=approvals,
        audit=audit,
        front_desk_thread_key=settings.front_desk_thread_key,
        memory=memory,
        memory_dir=settings.memory_dir,
        owner_name=settings.owner_name,
        distill_idle_seconds=settings.distill_idle_seconds,
        distill_model=settings.distill_model,
        calendar_enabled=settings.calendar_enabled,
        gcal_mcp_url=settings.gcal_mcp_url,
        owner_tz=settings.owner_tz,
    )
    adapter = TelegramAdapter(
        application=application,
        engine=manager,
        owner_id=settings.owner_telegram_id,
        guest_ack=settings.guest_ack,
        session_factory=session_factory,
        approvals=approvals,
        memory=memory,
    )
    return manager, adapter, policy, approvals, memory


async def serve(settings: Settings) -> None:
    """Bring up db + gate + engine + adapter on one loop and run until stopped."""
    engine: AsyncEngine = create_engine(settings.db_path)
    await init_db(engine)
    factory = session_factory(engine)
    application = Application.builder().token(settings.telegram_bot_token).build()
    manager, adapter, policy, approvals, memory = build_components(
        settings, application=application, session_factory=factory
    )
    # Seed NEVER/APPROVED before serving so the gate is correct from the first message.
    await policy.seed(
        never=[s.as_pair() for s in settings.never_seed],
        approved=[s.as_pair() for s in settings.approved_seed],
    )

    async def on_ready() -> None:
        await memory.ensure_scaffold()
        await memory.purge_expired()
        await manager.recover()
        await approvals.re_arm()

    try:
        await adapter.run(on_ready=on_ready)
    finally:
        await manager.shutdown()
        await engine.dispose()


def main() -> None:
    configure_logging()
    settings = load_settings()

    # The SDK's `claude` subprocess authenticates from CLAUDE_CODE_OAUTH_TOKEN in its
    # environment. Bridge the value here so it works whether the token arrived via a
    # Docker secret file or an env var. (config rejects ANTHROPIC_API_KEY, which would
    # otherwise outrank it and bill the API.)
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token

    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
