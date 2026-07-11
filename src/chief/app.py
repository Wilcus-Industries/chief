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
from .adapters.cli import CLI_LIMIT, CliAdapter, CliTaskIO
from .adapters.discord import DISCORD_LIMIT, DiscordAdapter, DiscordTaskIO
from .adapters.mirror import MirrorTaskIO
from .adapters.telegram import TELEGRAM_LIMIT, TelegramAdapter, TelegramTaskIO
from .client_plane import SocketServer
from .config import Settings
from .core import screening
from .core.backend import CopilotBackend
from .core.budget import (
    ACCUM_ADD,
    ACCUM_MAX,
    ACTION_DOWNGRADE,
    ACTION_PAUSE,
    BudgetGate,
    BudgetIO,
    CurrencyPolicy,
)
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
from .persistence import usage
from .persistence.db import create_engine, init_db, session_factory
from .persistence.messages import MessageLog
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
#: one file per secret field (telegram_bot_token, openrouter_api_key, …) — the
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

#: The split-vs-file output cap per platform (M8). The engine filters every query by
#: ``platform``, and each surface caps its message length differently: Telegram/Discord
#: at the chat limit, the CLI at the large socket-frame limit (#131).
_PLATFORM_LIMITS = {
    "telegram": TELEGRAM_LIMIT,
    "discord": DISCORD_LIMIT,
    "cli": CLI_LIMIT,
}


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
    return Settings()


def warn_if_classifier_degraded(settings: Settings) -> None:
    """Warn once at boot when the OpenRouter classifier path cannot work (#88).

    The cheap text classifiers (stop-intent steering, warrants-a-task auto-spawn, the
    complexity/routing judgments) and injection screening all run as direct OpenRouter
    one-shots now. Two ways they silently degrade — both fail safe, screening fails
    *open* (untrusted content passes UNSCREENED), and both are deliberate (owner
    decision D2: warn, never refuse to boot), so surface each loudly once:

    - **Keyless.** No ``openrouter_api_key`` ⇒ no HTTP call at all.
    - **Keyed but mis-namespaced.** ``classifier_model``/``screening_model`` are sent
      verbatim as OpenRouter's ``model`` field, which is namespaced (``vendor/model``).
      A bare id (e.g. ``claude-haiku-4-5``) makes OpenRouter reject *every* call, so the
      classifiers/screening fail exactly as if keyless — but with a key set there is no
      other signal. Name each offending field.
    """
    if settings.openrouter_api_key is None:
        logger.warning(
            "openrouter_api_key is not set — the cheap classifiers (stop-intent "
            "steering, warrants-a-task, complexity, routing) make no HTTP call and "
            "fail safe (no interrupt, no spawn, no escalation), and injection "
            "screening fails OPEN: untrusted web/browser/guest content reaches the "
            "agent UNSCREENED. Set openrouter_api_key to enable them."
        )
        return
    mis_namespaced = [
        name
        for name in ("classifier_model", "screening_model")
        if "/" not in getattr(settings, name)
    ]
    if mis_namespaced:
        logger.warning(
            "openrouter_api_key is set but %s lack(s) an OpenRouter namespace "
            "(expected 'vendor/model', e.g. 'anthropic/claude-haiku-4.5'). OpenRouter "
            "will reject every classifier call, so stop-intent steering, "
            "warrants-a-task, complexity, and routing fail safe and injection "
            "screening fails OPEN: untrusted content reaches the agent UNSCREENED. "
            "Fix the id(s): %s.",
            " and ".join(mis_namespaced),
            ", ".join(f"{n}={getattr(settings, n)!r}" for n in mis_namespaced),
        )


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


def build_harness_versioner(settings: Settings) -> Versioner:
    """Construct the harness dir's own versioner from settings (#110).

    A second, independent :class:`GitVersioner` rooted at ``harness_dir`` — the
    common parent of ``subagents_dir``/``chief_skills_dir`` — so every
    chief-authored subagent/skill write is its own revertible commit, separate
    from the memory repo's lock and history.

    It stages the whole root (``git add -A``), so the tracked set is subagents, skills,
    **and** ``self_config.yaml``, which defaults inside ``harness_dir`` (#125). That is
    intended: the overlay is chief-authored, reversible content of exactly the kind this
    versioner exists to make revertible. Anything else chief writes under the root is
    versioned on the same terms.
    """
    return (
        GitVersioner(
            settings.harness_dir,
            author_name=settings.git_author_name,
            author_email=settings.git_author_email,
        )
        if settings.harness_git
        else NullVersioner()
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
    """The per-currency usage-budget gate (#84), or ``None`` when budgeting is disabled.

    Builds one :class:`CurrencyPolicy` per native currency (#84): Copilot premium
    requests (a cumulative count vs ``premium_request_cap`` → pause) and OpenRouter
    dollars (additive spend vs ``openrouter_dollar_cap`` → downgrade). Like the
    scheduler, every warning and the choice card route to the owner inbox
    (``primary_thread_key``), so enabling it needs that key set.
    """
    if not settings.budget_enabled:
        return None
    assert settings.primary_thread_key is not None, (
        "budget_enabled requires primary_thread_key — budget warnings and the "
        "choice card have nowhere to land without it"
    )
    warn = settings.budget_warn_fractions
    exhaust = settings.budget_exhaust_fraction
    policies = {
        usage.PREMIUM_REQUESTS: CurrencyPolicy(
            cap=float(settings.premium_request_cap),
            warn_fractions=warn,
            exhaust_fraction=exhaust,
            accumulation=ACCUM_MAX,
            action=ACTION_PAUSE,
        ),
        usage.OPENROUTER_DOLLARS: CurrencyPolicy(
            cap=settings.openrouter_dollar_cap,
            warn_fractions=warn,
            exhaust_fraction=exhaust,
            accumulation=ACCUM_ADD,
            action=ACTION_DOWNGRADE,
        ),
    }
    return BudgetGate(
        session_factory=session_factory,
        io=io,
        owner_inbox=settings.primary_thread_key,
        policies=policies,
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
    harness_versioner: Versioner | None = None,
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
    # Every session is built through the Copilot backend (#88, chief's sole harness):
    # CopilotBackend.create_session is the SessionFactory the engine builds each
    # owner/guest session with.
    backend = CopilotBackend()
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
        # The cheap text classifiers (steering, warrants-task, complexity, routing) run
        # as direct OpenRouter one-shots (#88); this key authenticates them. None ⇒ they
        # make no call and fail safe.
        classifier_api_key=settings.openrouter_api_key,
        concurrency=settings.concurrency,
        turn_timeout=settings.turn_timeout_seconds,
        idle_archive_seconds=settings.idle_archive_seconds,
        compaction_idle_seconds=settings.compaction_idle_seconds,
        # The cap that decides split-vs-file output differs per platform (M8, #131).
        message_limit=_PLATFORM_LIMITS[platform],
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
        # A plain-quota owner turn spends the backend's native currency (#84): the
        # Copilot backend burns premium requests (#88 removed the Max-bridge backend
        # whose turns were informational-only).
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
        # chief-authored skills root (#106, part of #103): scanned fresh at every owner
        # spawn; absent/empty is inert (resolves to no extra dirs).
        chief_skills_dir=settings.chief_skills_dir,
        # Owner-only category-routed subagents (#87). Inert unless the flag is on; the
        # model each subagent runs on is resolved through the routing table at spawn.
        # subagents_dir (#105, part of #103) is the sole source: the built-ins are
        # scaffolded there on first boot (empty dir only), no restart needed after.
        subagents_enabled=settings.subagents_enabled,
        subagents_dir=settings.subagents_dir,
        # Share the exact versioner the memory store uses so auto-commit and memory
        # mutations go through the same git instance. The versioner self-serializes
        # all callers via its internal asyncio.Lock (#22 / #29).
        versioner=memory.versioner,
        # Separate versioner over data/harness/ (#110): its own root and its own
        # asyncio.Lock mean harness and memory commits never collide, even though
        # both fire at the end of the same turn.
        harness_versioner=harness_versioner,
        # Screenshot delivery (issue #34): when playwright is enabled, pass the
        # screenshots dir so the PostToolUse hook can read files and send_file them.
        screenshots_dir=(
            settings.playwright_screenshots_dir
            if settings.playwright_enabled
            else None
        ),
        # Untrusted-content screening (host-native): web/browser tool results and the
        # guest relay get a cheap OpenRouter injection screen; inert (None) when
        # disabled. The OpenRouter key authenticates the screen call — keyless, it fails
        # open (content passes unscreened); the boot warns once (#88).
        screener=(
            functools.partial(
                screening.screen_text,
                model=settings.screening_model,
                api_key=settings.openrouter_api_key,
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
    harness_versioner: Versioner | None = None,
    socket_server: SocketServer | None = None,
) -> Stack:
    """Build the Telegram engine stack against the shared gate/memory singletons.

    The one ``TelegramTaskIO`` doubles as the engine's ``TaskIO`` and the approval
    ``ApprovalIO`` (it implements both), so cards post through the same bot. Only called
    when ``settings.telegram_configured`` — the asserts narrow the optional secrets.

    #133: the engine's ``TaskIO`` is wrapped in :class:`MirrorTaskIO`, so every task
    milestone/reply this stack emits is mirrored onto the client-plane socket + recorded
    in the message log. Only the engine io is wrapped — the approval manager and budget
    keep the raw ``TelegramTaskIO`` (cards are #136's slice, budget stays unmirrored).
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
    mirror = MirrorTaskIO(
        io,
        platform="telegram",
        session_factory=session_factory,
        server=socket_server,
    )
    manager = build_engine(
        settings,
        platform="telegram",
        io=mirror,
        session_factory=session_factory,
        policy=policy,
        approvals=approvals,
        audit=audit,
        memory=memory,
        budget=build_budget(settings, io=io, session_factory=session_factory),
        routing=routing,
        harness_versioner=harness_versioner,
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
    harness_versioner: Versioner | None = None,
    socket_server: SocketServer | None = None,
) -> Stack:
    """Build the Discord engine stack — the Telegram stack's twin on the shared gate.

    Needs the privileged **message_content** intent (enable it in the Developer Portal).
    Only called when ``settings.discord_configured``; the asserts narrow the secrets.

    #133: like the Telegram stack, the engine's ``TaskIO`` is wrapped in
    :class:`MirrorTaskIO` so its outbound mirrors onto the socket + the message log; the
    approval manager and budget keep the raw ``DiscordTaskIO``.
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
    mirror = MirrorTaskIO(
        io,
        platform="discord",
        session_factory=session_factory,
        server=socket_server,
    )
    manager = build_engine(
        settings,
        platform="discord",
        io=mirror,
        session_factory=session_factory,
        policy=policy,
        approvals=approvals,
        audit=audit,
        memory=memory,
        budget=build_budget(settings, io=io, session_factory=session_factory),
        routing=routing,
        harness_versioner=harness_versioner,
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


def build_cli_stack(
    settings: Settings,
    *,
    socket_server: SocketServer,
    session_factory: async_sessionmaker[AsyncSession],
    policy: PolicyStore,
    audit: AuditLog,
    memory: MemoryStore,
    routing: RoutingStore | None = None,
    harness_versioner: Versioner | None = None,
) -> Stack:
    """Build the CLI engine stack bound to the always-on client-plane socket (#131).

    The always-on plane (no ``cli_configured`` gate): the socket is the CLI's transport,
    so this stack is built unconditionally — a tokenless boot still yields one (cli)
    stack. The one :class:`CliTaskIO` doubles as the engine's ``TaskIO`` and approval
    ``ApprovalIO`` (like the chat stacks), so cards broadcast out the same socket as
    replies. ``socket_server`` is constructed by ``serve`` *before* this call so the
    adapter can install its inbound handler before the server is run.

    Both sides share one :class:`MessageLog` (#132): the ``CliTaskIO`` records both
    directions and powers detach-replay — a frame emitted while no client is attached is
    logged held, then replayed by the adapter's connect hook on the next attach.

    The engine io is additionally wrapped in :class:`MirrorTaskIO` (#133) with
    ``server=None`` — the inner ``CliTaskIO`` already broadcasts (the socket *is* its
    delivery), so the mirror adds only its own message-log record, never a duplicate
    frame. The approval manager and budget keep the raw ``CliTaskIO``.

    """
    message_log = MessageLog(session_factory)
    io = CliTaskIO(socket_server, log=message_log)
    approvals = ApprovalManager(
        session_factory=session_factory,
        io=io,
        policy=policy,
        audit=audit,
        timeout_seconds=settings.approval_timeout_seconds,
    )
    mirror = MirrorTaskIO(
        io, platform="cli", session_factory=session_factory, server=None
    )
    manager = build_engine(
        settings,
        platform="cli",
        io=mirror,
        session_factory=session_factory,
        policy=policy,
        approvals=approvals,
        audit=audit,
        memory=memory,
        budget=build_budget(settings, io=io, session_factory=session_factory),
        routing=routing,
        harness_versioner=harness_versioner,
    )
    adapter = CliAdapter(
        server=socket_server, engine=manager, memory=memory, log=message_log
    )
    return manager, adapter, approvals


def build_stacks(
    settings: Settings,
    *,
    socket_server: SocketServer,
    session_factory: async_sessionmaker[AsyncSession],
    policy: PolicyStore,
    audit: AuditLog,
    memory: MemoryStore,
    routing: RoutingStore | None = None,
    harness_versioner: Versioner | None = None,
) -> list[Stack]:
    """Build one engine stack per configured platform, plus the always-on CLI stack.

    Telegram and Discord are gated on their tokens; the CLI stack is unconditional (the
    socket is always-on infrastructure, #131) and appended last, so even a zero-token
    boot yields exactly one — the CLI — stack. #133: ``socket_server`` threads into
    every stack builder so each stack's outbound is mirrored onto the socket + log.
    """
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
                harness_versioner=harness_versioner,
                socket_server=socket_server,
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
                harness_versioner=harness_versioner,
                socket_server=socket_server,
            )
        )
    stacks.append(
        build_cli_stack(
            settings,
            socket_server=socket_server,
            session_factory=session_factory,
            policy=policy,
            audit=audit,
            memory=memory,
            routing=routing,
            harness_versioner=harness_versioner,
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
        # #88 HIGH-2: agent monitors get the chief_web read surface (fetch + search),
        # the same in-process server owner sessions use. None when web tools are off.
        web_service=build_web_service(settings),
        monitor_model=settings.monitor_model,
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

    The always-on client-plane socket listener (#130) runs unconditionally alongside the
    stacks — a chief with zero platform tokens still boots and stays alive on just the
    socket, which is removed on clean shutdown.
    """
    warn_if_classifier_degraded(settings)
    engine: AsyncEngine = create_engine(settings.db_path)
    await init_db(engine)
    factory = session_factory(engine)
    audit = AuditLog(settings.audit_log_path)
    policy = PolicyStore(factory, audit=audit)
    memory = build_memory(settings)
    # Second, independent versioner over data/harness/ (#110) — its own root and lock
    # mean a harness commit never collides with a memory commit in the same turn.
    harness_versioner = build_harness_versioner(settings)
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
    await harness_versioner.init()

    # #130 unconditional client-plane listener — constructed BEFORE the stacks so the
    # #131 CLI stack can bind to it (the CliAdapter installs its inbound handler in its
    # ctor; construction ≠ binding, so this order resolves the cycle: the handler is in
    # place before socket_server.run() is gathered below). It also keeps the gather (and
    # the process) alive on a zero-platform, no-scheduler boot, where every other
    # long-lived coro is absent.
    socket_server = SocketServer(settings.socket_path)
    stacks = build_stacks(
        settings,
        socket_server=socket_server,
        session_factory=factory,
        policy=policy,
        audit=audit,
        memory=memory,
        routing=routing,
        harness_versioner=harness_versioner,
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
    # Unconditional (#130): a socket-only chief has no other coro to keep gather alive.
    coros.append(socket_server.run())

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
        # Close the client-plane listener (removes the socket file) before disposing the
        # engine; guarded so a stop failure can't mask the dispose (#130).
        try:
            await socket_server.stop()
        except Exception:
            logger.exception("socket server stop failed during shutdown")
        await engine.dispose()


def main() -> None:
    configure_logging()
    settings = load_settings()
    # The Copilot CLI authenticates from its own ``~/.copilot/config.json`` (#88), so
    # there is no SDK auth token to bridge into the environment here.
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
