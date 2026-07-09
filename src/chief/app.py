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
import functools
import json
import logging
import os
import urllib.request
from pathlib import Path

import discord
import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram.ext import Application

from .adapters.base import Adapter, ReadyHook
from .adapters.discord import DISCORD_LIMIT, DiscordAdapter, DiscordTaskIO
from .adapters.telegram import TELEGRAM_LIMIT, TelegramAdapter, TelegramTaskIO
from .config import Settings
from .core import screening
from .core.backend import select_backend
from .core.budget import BudgetGate, BudgetIO
from .core.copilot_session import openrouter_provider_config
from .core.routing import RoutingStore
from .core.scheduler import Scheduler
from .core.tasks import TaskIO, TaskManager
from .gate.approvals import ApprovalManager
from .gate.blacklist import Blacklist
from .gate.policy import PolicyStore
from .memory.markdown_backend import MarkdownMemory
from .memory.store import MemoryStore
from .memory.versioning import GitVersioner, NullVersioner, Versioner
from .obs.audit import AuditLog
from .obs.logging import configure_logging
from .persistence.db import create_engine, init_db, session_factory
from .tools.browser import mcp as browser_mcp
from .tools.calendar import mcp as calendar_mcp
from .tools.drive import mcp as drive_mcp
from .tools.gmail import mcp as gmail_mcp
from .tools.google import GoogleService
from .tools.google.add_account_service import AddAccountService
from .tools.google.auth import _USERINFO_URL
from .tools.google.list_accounts_service import ListAccountsService
from .tools.google.set_account_service import SetAccountService
from .tools.guest import GuestAdminService
from .tools.routing_admin import RoutingAdminService
from .tools.schedule import ScheduleBashService, ScheduleService
from .tools.sheets import mcp as sheets_mcp
from .tools.shell import ShellService
from .tools.web import BraveSearcher, WebFetcher, WebService

logger = logging.getLogger("chief.app")

#: Candidate secrets directories, first existing one wins (host-native). Each holds
#: one file per secret field (telegram_bot_token, claude_code_oauth_token, …) — the
#: pydantic-settings ``secrets_dir`` convention the old Docker mount used. The
#: CHIEF_SECRETS_DIR env var overrides; env vars alone also work (no dir needed).
SECRETS_DIR_CANDIDATES = (
    os.path.expanduser("~/.config/chief/secrets"),
    "secrets",
)

# The bundled skills plugin (M10) lives at repo-root vendor/chief-skills. The owner
# session runs with cwd=memory_dir and the CLI resolves --plugin-dir against that cwd,
# so the path must be absolute — resolved here against the process cwd, the same
# convention config.yaml is loaded by.
SKILLS_PLUGIN_DIR = "vendor/chief-skills"

#: One built engine stack for a platform: its engine, its adapter, and its approval
#: manager (the adapter's ``run`` drives the connection; the manager needs shutdown).
Stack = tuple[TaskManager, Adapter, ApprovalManager]


def load_settings() -> Settings:
    """Load settings, using the first existing secrets dir and env otherwise.

    ``CHIEF_SECRETS_DIR`` overrides the candidate list (``~/.config/chief/secrets``,
    then the repo-local ``./secrets``). Skipping the secrets source when no dir exists
    avoids a noisy "directory does not exist" warning on env-only runs.
    """
    override = os.environ.get("CHIEF_SECRETS_DIR")
    candidates = (override,) if override else SECRETS_DIR_CANDIDATES
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return Settings(_secrets_dir=candidate)  # type: ignore[call-arg]
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


def build_google_services(settings: Settings) -> list[GoogleService]:
    """Resolve the enabled Google MCP servers from settings (owner-only at runtime)."""
    services: list[GoogleService] = []
    if settings.calendar_enabled:
        services.append(calendar_mcp.service(settings.calendar_mcp_url))
    if settings.drive_enabled:
        services.append(drive_mcp.service(settings.drive_mcp_url))
    if settings.sheets_enabled:
        services.append(sheets_mcp.service(settings.sheets_mcp_url))
    if settings.gmail_enabled:
        services.append(gmail_mcp.chief_service(settings.gmail_mcp_url))
    if settings.playwright_enabled:
        services.append(browser_mcp.service(settings.playwright_mcp_url))
    return services


def build_shell_service(settings: Settings) -> ShellService | None:
    """The host shell service (M7, host-native), or ``None`` when disabled."""
    if not settings.shell_enabled:
        return None
    return ShellService(
        workspace_dir=settings.workspace_dir,
        timeout_seconds=settings.shell_timeout_seconds,
        output_limit=settings.shell_output_limit,
    )


def build_web_service(settings: Settings) -> WebService | None:
    """The chief-owned web fetch/search tools (#81), or ``None`` when disabled.

    One in-process ``chief_web`` server reaches both backends. The fetcher is bounded by
    the configured timeout + byte cap and guards every URL against SSRF; the searcher
    uses the Brave API key (optional — search degrades to a note without it).
    """
    if not settings.web_tools_enabled:
        return None
    return WebService(
        fetcher=WebFetcher(
            timeout=settings.web_fetch_timeout_seconds,
            max_bytes=settings.web_fetch_max_bytes,
        ),
        searcher=BraveSearcher(
            api_key=settings.brave_search_api_key,
            count=settings.web_search_count,
            timeout=settings.web_fetch_timeout_seconds,
        ),
    )


def build_routing_admin_service(
    routing: RoutingStore | None,
) -> RoutingAdminService | None:
    """The self-config routing tool (#83), or ``None`` when routing is off.

    Shares the one live :class:`RoutingStore` the engine resolves against, so an edit is
    effective at the next spawn. Owner-only; its mutating verbs are gated through
    ``blacklist_tools`` (:mod:`chief.config`). Inert (``None``) unless routing is wired.
    """
    if routing is None:
        return None
    return RoutingAdminService(routing=routing)


def build_guest_calendar_service(settings: Settings) -> GoogleService | None:
    """The narrowed calendar service for guest sessions (M6), or ``None``.

    Guests get free/busy + an approval-gated booking only when both guests and the
    calendar are enabled; otherwise the receptionist degrades to take-a-message.
    """
    if not (settings.guest_enabled and settings.calendar_enabled):
        return None
    return calendar_mcp.guest_service(settings.calendar_mcp_url)


def _resolve_email_from_token(token_path: Path) -> str | None:
    """Resolve the Google email for a legacy token that lacks an ``account`` field.

    Read-only: refreshes the token **in memory** to obtain a fresh access token, then
    calls the Google userinfo endpoint.  Never writes back to ``token_path``.  Returns
    the email string, or ``None`` on any failure (missing fields, network error, etc.)
    so the caller can gracefully degrade to the filename-slug label.

    This is the production resolver passed to
    :func:`~chief.tools.google.accounts.discover_accounts` by
    :func:`build_list_accounts_service`.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        raw = json.loads(token_path.read_text(encoding="utf-8"))
        creds = Credentials.from_authorized_user_info(raw)  # type: ignore[no-untyped-call]
        if not creds.valid:
            creds.refresh(Request())
        access_token = creds.token
        if not access_token:
            logger.warning("no access token after refresh for %s", token_path)
            return None
        req = urllib.request.Request(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data: dict[str, object] = json.loads(resp.read().decode())
        email = data.get("email")
        return str(email) if email else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("email resolver failed for %s: %s", token_path, exc)
        return None


def build_list_accounts_service(
    secrets_dir: Path | str = Path("secrets/google_tokens"),
) -> ListAccountsService:
    """Build the owner-only list_accounts tool backed by dynamic token-dir re-scan.

    Scans ``secrets_dir`` (defaults to the repo-local ``secrets/google_tokens``) for
    ``google_token*.json`` files — the same host directory the MCP containers
    bind-mount at ``/token``, so both sides see the same accounts.
    Always returns a :class:`ListAccountsService` — with an empty account list when the
    dir is absent or empty — so the tool is available even when no Google account has
    been set up yet.

    Dynamic mode (issue #50): passes ``secrets_dir`` through to
    :class:`~chief.tools.google.list_accounts_service.ListAccountsService` so the
    tool re-scans the directory on every call.  A token dropped at runtime appears in
    ``list_accounts`` without a restart.

    Pass ``secrets_dir=tmp_path`` in tests to avoid scanning the real filesystem.
    ``_resolve_email_from_token`` is the production resolver: it refreshes the token
    in memory and calls the Google userinfo endpoint to recover the email for legacy
    tokens that lack the ``account`` field.  It degrades to ``None`` on failure.
    """
    return ListAccountsService(
        secrets_dir=Path(secrets_dir),
        email_resolver=_resolve_email_from_token,
    )


def build_set_account_service(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    platform: str,
    secrets_dir: Path | str = Path("secrets/google_tokens"),
) -> SetAccountService:
    """Build the owner-only ``set_account`` tool backed by dynamic token-dir re-scan.

    Dynamic mode (issue #50): passes ``secrets_dir`` through to
    :class:`~chief.tools.google.set_account_service.SetAccountService` so the tool
    re-scans the directory on every call.  A token dropped at runtime is immediately
    selectable via ``set_account`` without a restart.
    Always returns a service — with an empty account list when the dir is absent —
    so the tool is available even before any Google account has been set up.
    """
    return SetAccountService(
        session_factory=session_factory,
        platform=platform,
        secrets_dir=Path(secrets_dir),
        email_resolver=_resolve_email_from_token,
    )


def build_add_account_service(
    secrets_dir: Path | str = Path("secrets/google_tokens"),
    client_secrets: Path | str = Path("secrets/google_oauth_client.json"),
) -> AddAccountService:
    """Build the owner-only ``add_account`` tool — runtime chat-consent flow (#53).

    Writes the minted token into ``secrets_dir`` (the same dir ``list_accounts`` /
    ``set_account`` re-scan on every call), so a newly added account is selectable
    with no restart. ``client_secrets`` is the shared Desktop-app OAuth client JSON
    under the repo-local ``secrets/`` dir. The default production OAuth seams hit
    Google's token + userinfo endpoints; tests inject their own.
    """
    return AddAccountService(
        secrets_dir=Path(secrets_dir),
        client_secrets=Path(client_secrets),
    )


def build_budget(
    settings: Settings,
    *,
    io: BudgetIO,
    session_factory: async_sessionmaker[AsyncSession],
) -> BudgetGate | None:
    """The usage-budget gate (M9), or ``None`` when budgeting is disabled.

    Like the scheduler, the budget routes every warning and the choice card to the
    owner inbox (``primary_thread_key``), so enabling it requires that key be set —
    asserted here (there is no config validator, since the gate is inert when off).
    """
    if not settings.budget_enabled:
        return None
    assert settings.primary_thread_key is not None, (
        "budget_enabled requires primary_thread_key — budget warnings and the "
        "choice card have nowhere to land without it"
    )
    return BudgetGate(
        session_factory=session_factory,
        io=io,
        owner_inbox=settings.primary_thread_key,
        monthly_credit_usd=settings.monthly_credit_usd,
        warn_fractions=settings.budget_warn_fractions,
        exhaust_fraction=settings.budget_exhaust_fraction,
        owner_tz=settings.owner_tz,
        anchor_day=settings.budget_cycle_anchor_day,
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
    budget: BudgetGate | None = None,
    routing: RoutingStore | None = None,
) -> TaskManager:
    """Build a platform-bound ``TaskManager`` (every query filters by ``platform``)."""
    guest_admin = (
        GuestAdminService(session_factory=session_factory, platform=platform)
        if settings.guest_enabled
        else None
    )
    # Account registry: always wired into owner sessions (read-only, no card).
    # Re-scans the tokens dir per call so new tokens appear with no restart.
    list_accounts = build_list_accounts_service()
    # Per-thread active-account binding: owner-only, pre-approved (no card).
    set_account = build_set_account_service(
        session_factory=session_factory,
        platform=platform,
    )
    # Runtime add-account via chat consent (issue #53): owner-only, no card. Writes
    # the minted token into the same /token dir the registry re-scans, so a new
    # account is selectable immediately — no redeploy.
    add_account = build_add_account_service()
    # The owner's schedule tools are platform-agnostic, so every stack's owner session
    # gets them when the scheduler is on; only the *loop* (built in serve) is singular.
    schedule = (
        ScheduleService(
            session_factory=session_factory,
            owner_tz=settings.owner_tz,
            monitor_min_interval_seconds=settings.monitor_min_interval_seconds,
        )
        if settings.scheduler_enabled
        else None
    )
    schedule_bash = (
        ScheduleBashService(
            session_factory=session_factory,
            owner_tz=settings.owner_tz,
            monitor_min_interval_seconds=settings.monitor_min_interval_seconds,
        )
        if settings.scheduler_enabled
        else None
    )
    # Route every session through the config-selected AgentBackend (#75). Only
    # ``claude`` is valid today; ClaudeBackend.create_session is the SessionFactory the
    # engine builds each owner/guest session with.
    backend = select_backend(settings.agent_backend)
    return TaskManager(
        session_factory=session_factory,
        io=io,
        platform=platform,
        session_factory_sdk=backend.create_session,
        owner_model=settings.owner_model_default,
        # Owner Opus escalation (M11): /opus pins a thread to owner_model_opus; with
        # opus_auto_detect on, a complex owner turn also asks before escalating.
        owner_model_opus=settings.owner_model_opus,
        opus_auto_detect=settings.opus_auto_detect,
        # Model routing (#79): the shared, seeded routing table (None when disabled),
        # the OpenRouter BYOK provider built once from settings (only an openrouter
        # target consumes it), and the per-surface category defaults.
        routing=routing,
        openrouter_provider=(
            openrouter_provider_config(settings) if settings.routing_enabled else None
        ),
        routing_surface_defaults=settings.routing_surface_defaults,
        classifier_model=settings.classifier_model,
        concurrency=settings.concurrency,
        turn_timeout=settings.turn_timeout_seconds,
        idle_archive_seconds=settings.idle_archive_seconds,
        compaction_idle_seconds=settings.compaction_idle_seconds,
        # The cap that decides split-vs-file output differs per platform (M8).
        message_limit=TELEGRAM_LIMIT if platform == "telegram" else DISCORD_LIMIT,
        policy=policy,
        approvals=approvals,
        audit=audit,
        # Owner default-allow posture: only these patterns/tools still raise a card.
        blacklist=Blacklist.from_config(
            settings.blacklist_shell_patterns, settings.blacklist_tools
        ),
        front_desk_thread_key=settings.front_desk_thread_key,
        memory=memory,
        memory_dir=settings.memory_dir,
        owner_name=settings.owner_name,
        google_services=build_google_services(settings),
        owner_tz=settings.owner_tz,
        shell_service=build_shell_service(settings),
        # chief-owned web fetch/search (#81) — owner-only, SSRF-guarded, one server for
        # both backends. None when web_tools_enabled is off.
        web_service=build_web_service(settings),
        # Self-config routing tool (#83) — owner-only, shares the live routing table;
        # its edits are gated via blacklist_tools. None when routing is off.
        routing_admin_service=build_routing_admin_service(routing),
        workspace_dir=(
            settings.workspace_dir if settings.workspace_enabled else None
        ),
        guest_model=settings.guest_model,
        guest_calendar_service=build_guest_calendar_service(settings),
        guest_admin_service=guest_admin,
        list_accounts_service=list_accounts,
        set_account_service=set_account,
        add_account_service=add_account,
        schedule_service=schedule,
        schedule_bash_service=schedule_bash,
        # When budgeting is on, the gate records each turn's spend and the manager
        # enforces its mode (pause/downgrade); both are inert (None) otherwise.
        budget=budget,
        # The owner's private DM (primary_thread_key) — where the budget card lands AND
        # where an owner-in-group tool approval is DM'd (M11). Wired whenever either
        # subsystem needs it, so the group approval route never falls open to the room.
        owner_inbox=(
            settings.primary_thread_key
            if (budget is not None or settings.group_chat_enabled)
            else None
        ),
        budget_downgrade_model=(
            settings.budget_downgrade_model if budget is not None else None
        ),
        # Group chats (M11): cap on the per-group ambient buffer.
        group_context_max_messages=settings.group_context_max_messages,
        # Owner-only packaged skills (M10). The path must be absolute (see
        # SKILLS_PLUGIN_DIR) — the owner session's cwd=memory_dir would mis-resolve a
        # relative --plugin-dir. Inert (None path) unless the flag is on.
        skills_enabled=settings.skills_enabled,
        skills_plugin_path=(
            os.path.abspath(SKILLS_PLUGIN_DIR) if settings.skills_enabled else None
        ),
        default_skills=settings.default_skills,
        # Share the exact versioner the memory store uses so auto-commit and memory
        # mutations go through the same git instance. The versioner self-serializes
        # all callers via its internal asyncio.Lock (#22 / #29).
        versioner=memory.versioner,
        # Screenshot delivery (issue #34): when playwright is enabled, pass the
        # screenshots dir so the PostToolUse hook can read files and send_file them.
        screenshots_dir=(
            settings.playwright_screenshots_dir
            if settings.playwright_enabled
            else None
        ),
        # Untrusted-content screening (host-native): web/browser tool results and the
        # guest relay get a cheap Haiku injection screen; inert (None) when disabled.
        screener=(
            functools.partial(
                screening.screen_text, model=settings.screening_model
            )
            if settings.screening_enabled
            else None
        ),
        screening_tools=settings.screening_tools,
        screening_block=settings.screening_block,
    )


def build_telegram_stack(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    policy: PolicyStore,
    audit: AuditLog,
    memory: MemoryStore,
    routing: RoutingStore | None = None,
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
        budget=build_budget(settings, io=io, session_factory=session_factory),
        routing=routing,
    )
    adapter = TelegramAdapter(
        application=application,
        engine=manager,
        owner_id=settings.owner_telegram_id,
        guest_ack=settings.guest_ack,
        session_factory=session_factory,
        approvals=approvals,
        memory=memory,
        io=io,
        guest_enabled=settings.guest_enabled,
        front_desk_thread_key=settings.front_desk_thread_key,
        guest_rate=settings.guest_rate_per_window,
        guest_rate_window=settings.guest_rate_window_seconds,
        guest_global_rate=settings.guest_global_rate_per_window,
        group_chat_enabled=settings.group_chat_enabled,
        owner_home_chat_id=settings.owner_home_chat_id,
    )
    return manager, adapter, approvals


def build_discord_stack(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    policy: PolicyStore,
    audit: AuditLog,
    memory: MemoryStore,
    routing: RoutingStore | None = None,
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
        budget=build_budget(settings, io=io, session_factory=session_factory),
        routing=routing,
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
        io=io,
        guest_enabled=settings.guest_enabled,
        front_desk_thread_key=settings.front_desk_thread_key,
        guest_rate=settings.guest_rate_per_window,
        guest_rate_window=settings.guest_rate_window_seconds,
        guest_global_rate=settings.guest_global_rate_per_window,
        group_chat_enabled=settings.group_chat_enabled,
        owner_home_guild_id=settings.owner_home_guild_id,
    )
    return manager, adapter, approvals


def build_stacks(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    policy: PolicyStore,
    audit: AuditLog,
    memory: MemoryStore,
    routing: RoutingStore | None = None,
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
                routing=routing,
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
                routing=routing,
            )
        )
    return stacks


def build_scheduler(
    settings: Settings,
    *,
    stacks: list[Stack],
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[Scheduler | None, httpx.AsyncClient | None]:
    """Build the single scheduler loop, bound to the primary platform's stack.

    Returns ``(None, None)`` when the scheduler is disabled. The scheduler is singular
    (one tick loop), so it binds to the one stack whose manager drives
    ``settings.primary_platform`` — that manager's IO is where reminders, wakeups, and
    heartbeat alerts land, and it is the :class:`Waker` a ``wakeup`` fire boots a turn
    through. The owned heartbeat http client (a dead-man's-switch GET) is returned
    alongside so ``serve`` can close it on teardown; ``None`` when no ``heartbeat_url``.
    """
    if not settings.scheduler_enabled:
        return None, None
    # Both guaranteed by the scheduler config validator (config._require_primary_*).
    assert settings.primary_thread_key is not None
    primary = next(
        (s for s in stacks if s[0].platform == settings.primary_platform), None
    )
    assert primary is not None, (
        f"scheduler_enabled but no stack for primary_platform="
        f"{settings.primary_platform!r}"
    )
    manager = primary[0]
    # Only build the client when there's a url to ping — else the engine no-ops the
    # heartbeat. httpx.AsyncClient.get(url, timeout=) satisfies the HttpClient Protocol.
    http = httpx.AsyncClient() if settings.heartbeat_url else None
    scheduler = Scheduler(
        session_factory=session_factory,
        io=manager.io,
        waker=manager,
        primary_thread_key=settings.primary_thread_key,
        shell_service=build_shell_service(settings),
        google_services=build_google_services(settings),
        classifier_model=settings.classifier_model,
        owner_tz=settings.owner_tz,
        quiet_hours_start=settings.quiet_hours_start,
        quiet_hours_end=settings.quiet_hours_end,
        tick_seconds=settings.scheduler_tick_seconds,
        heartbeat_url=settings.heartbeat_url,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        http=http,
    )
    return scheduler, http


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
    # Model routing (#79): one shared, seeded routing table across all stacks (None when
    # disabled). Seeded from config below, before serving, like the policy lists.
    routing = RoutingStore(factory) if settings.routing_enabled else None

    # Seed NEVER/APPROVED + the routing table + scaffold memory before serving so all
    # are correct from the first message (no live connection; once across platforms).
    await policy.seed(
        never=[s.as_pair() for s in settings.never_seed],
        approved=[s.as_pair() for s in settings.approved_seed],
    )
    if routing is not None:
        await routing.seed(s.as_tuple() for s in settings.routing_seed)
    await memory.ensure_scaffold()
    await memory.purge_expired()

    stacks = build_stacks(
        settings,
        session_factory=factory,
        policy=policy,
        audit=audit,
        memory=memory,
        routing=routing,
    )
    # One scheduler loop across all stacks (it binds to the primary platform's manager),
    # owning an http client for the heartbeat when configured. None when disabled.
    scheduler, http = build_scheduler(
        settings, stacks=stacks, session_factory=factory
    )

    def make_ready(manager: TaskManager, approvals: ApprovalManager) -> ReadyHook:
        async def on_ready() -> None:
            await manager.recover()
            await approvals.re_arm()

        return on_ready

    coros = [
        adapter.run(on_ready=make_ready(manager, approvals))
        for manager, adapter, approvals in stacks
    ]
    if scheduler is not None:
        coros.append(scheduler.run())

    try:
        await asyncio.gather(*coros)
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
        # The Scheduler owns no closable resource but the heartbeat http client; close
        # it (guarded, like the adapter stops) before disposing the engine.
        if http is not None:
            try:
                await http.aclose()
            except Exception:
                logger.exception("heartbeat http close failed during shutdown")
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
