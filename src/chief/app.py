"""Entrypoint — wire settings, logging, db, the task engine, and the adapters, then run.

Run with ``python -m chief.app``. Everything runs on one asyncio loop
(:func:`asyncio.run`): schema init, each engine's per-task turns/timers, and every
adapter's connection all share it. The DB, gate, audit, and memory are built **once**
and shared; each configured chat platform (Telegram and/or Discord) then gets its own
engine stack (its IO, approval manager, platform-bound ``TaskManager``, and adapter),
since the engine filters every query by ``platform``. Per-platform restart recovery runs
once that platform's connection is live (``on_ready``), so it can ping the owner about
tasks left mid-flight.
"""

import asyncio
import logging
import os

import discord
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram.ext import Application

from .adapters.base import Adapter, ReadyHook
from .adapters.discord import DiscordAdapter, DiscordTaskIO
from .adapters.telegram import TelegramAdapter, TelegramTaskIO
from .config import Settings
from .core.tasks import TaskIO, TaskManager
from .gate.approvals import ApprovalManager
from .gate.policy import PolicyStore
from .memory.markdown_backend import MarkdownMemory
from .memory.store import MemoryStore
from .memory.versioning import GitVersioner, NullVersioner, Versioner
from .obs.audit import AuditLog
from .obs.logging import configure_logging
from .persistence.db import create_engine, init_db, session_factory

logger = logging.getLogger("chief.app")

DOCKER_SECRETS_DIR = "/run/secrets"

#: One built engine stack for a platform: its engine, its adapter, and its approval
#: manager (the adapter's ``run`` drives the connection; the manager needs shutdown).
Stack = tuple[TaskManager, Adapter, ApprovalManager]


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


def build_engine(
    settings: Settings,
    *,
    platform: str,
    io: TaskIO,
    session_factory: async_sessionmaker[AsyncSession],
    policy: PolicyStore,
    approvals: ApprovalManager,
    audit: AuditLog,
    memory: MemoryStore,
) -> TaskManager:
    """Build a platform-bound ``TaskManager`` (every query filters by ``platform``)."""
    return TaskManager(
        session_factory=session_factory,
        io=io,
        platform=platform,
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


def build_telegram_stack(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    policy: PolicyStore,
    audit: AuditLog,
    memory: MemoryStore,
) -> Stack:
    """Build the Telegram engine stack against the shared gate/memory singletons.

    The one ``TelegramTaskIO`` doubles as the engine's ``TaskIO`` and the approval
    ``ApprovalIO`` (it implements both), so cards post through the same bot. Only called
    when ``settings.telegram_configured`` — the asserts narrow the optional secrets.
    """
    assert settings.telegram_bot_token is not None
    assert settings.owner_telegram_id is not None
    application = Application.builder().token(settings.telegram_bot_token).build()
    io = TelegramTaskIO(application.bot)
    approvals = ApprovalManager(
        session_factory=session_factory,
        io=io,
        policy=policy,
        audit=audit,
        timeout_seconds=settings.approval_timeout_seconds,
    )
    manager = build_engine(
        settings,
        platform="telegram",
        io=io,
        session_factory=session_factory,
        policy=policy,
        approvals=approvals,
        audit=audit,
        memory=memory,
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
    return manager, adapter, approvals


def build_discord_stack(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    policy: PolicyStore,
    audit: AuditLog,
    memory: MemoryStore,
) -> Stack:
    """Build the Discord engine stack — the Telegram stack's twin on the shared gate.

    Needs the privileged **message_content** intent (enable it in the Developer Portal).
    Only called when ``settings.discord_configured``; the asserts narrow the secrets.
    """
    assert settings.discord_bot_token is not None
    assert settings.owner_discord_id is not None
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    io = DiscordTaskIO(client)
    approvals = ApprovalManager(
        session_factory=session_factory,
        io=io,
        policy=policy,
        audit=audit,
        timeout_seconds=settings.approval_timeout_seconds,
    )
    manager = build_engine(
        settings,
        platform="discord",
        io=io,
        session_factory=session_factory,
        policy=policy,
        approvals=approvals,
        audit=audit,
        memory=memory,
    )
    adapter = DiscordAdapter(
        client=client,
        token=settings.discord_bot_token,
        engine=manager,
        owner_id=settings.owner_discord_id,
        guest_ack=settings.guest_ack,
        session_factory=session_factory,
        approvals=approvals,
        memory=memory,
    )
    return manager, adapter, approvals


def build_stacks(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    policy: PolicyStore,
    audit: AuditLog,
    memory: MemoryStore,
) -> list[Stack]:
    """Build one engine stack per configured platform (Telegram and/or Discord)."""
    stacks: list[Stack] = []
    if settings.telegram_configured:
        stacks.append(
            build_telegram_stack(
                settings,
                session_factory=session_factory,
                policy=policy,
                audit=audit,
                memory=memory,
            )
        )
    if settings.discord_configured:
        stacks.append(
            build_discord_stack(
                settings,
                session_factory=session_factory,
                policy=policy,
                audit=audit,
                memory=memory,
            )
        )
    return stacks


async def serve(settings: Settings) -> None:
    """Bring up db + gate + memory + every configured platform stack, then run.

    The DB, policy, audit, and memory are shared singletons; each configured platform
    gets its own stack and runs concurrently (``asyncio.gather``). Boot-time setup that
    needs no live connection (policy seed, memory scaffold/purge) runs once up front;
    per-platform recovery + approval re-arm fire in each stack's ``on_ready``.
    """
    engine: AsyncEngine = create_engine(settings.db_path)
    await init_db(engine)
    factory = session_factory(engine)
    audit = AuditLog(settings.audit_log_path)
    policy = PolicyStore(factory, audit=audit)
    memory = build_memory(settings)

    # Seed NEVER/APPROVED + scaffold memory before serving so both are correct from the
    # first message (no live connection needed; done once across all platforms).
    await policy.seed(
        never=[s.as_pair() for s in settings.never_seed],
        approved=[s.as_pair() for s in settings.approved_seed],
    )
    await memory.ensure_scaffold()
    await memory.purge_expired()

    stacks = build_stacks(
        settings,
        session_factory=factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    def make_ready(manager: TaskManager, approvals: ApprovalManager) -> ReadyHook:
        async def on_ready() -> None:
            await manager.recover()
            await approvals.re_arm()

        return on_ready

    try:
        await asyncio.gather(
            *(
                adapter.run(on_ready=make_ready(manager, approvals))
                for manager, adapter, approvals in stacks
            )
        )
    finally:
        # gather propagates the first failure without cancelling its siblings, so one
        # adapter crashing leaves the others' connections live — close every adapter
        # before disposing the engine. Guard each stop so one failure can't mask the
        # rest or skip the engine dispose.
        for manager, adapter, _approvals in stacks:
            try:
                await adapter.stop()
            except Exception:
                logger.exception("adapter stop failed during shutdown")
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
