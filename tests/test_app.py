"""Entrypoint settings loading and per-platform component wiring."""

import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief import app
from chief.adapters.discord import DiscordAdapter, DiscordTaskIO
from chief.adapters.telegram import TelegramAdapter, TelegramTaskIO
from chief.config import PolicySeed, Settings
from chief.core.budget import BudgetGate
from chief.gate.policy import PolicyStore
from chief.memory.store import MemoryStore
from chief.memory.versioning import GitVersioner, NullVersioner
from chief.obs.audit import AuditLog
from chief.persistence import approvals as appr_repo
from chief.persistence import policy as policy_repo
from chief.persistence import usage


def test_load_settings_uses_env_when_no_secrets_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 5\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-tg")
    monkeypatch.setattr(
        app, "SECRETS_DIR_CANDIDATES", (str(tmp_path / "absent"),)
    )

    settings = app.load_settings()

    assert settings.owner_telegram_id == 5
    assert settings.telegram_bot_token == "env-tg"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        owner_telegram_id=42,
        telegram_bot_token="x:y",
        classifier_model="claude-haiku-4-5",
        memory_git=False,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _shared(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> tuple[PolicyStore, AuditLog, MemoryStore]:
    """The singletons built once and shared across every platform stack."""
    audit = AuditLog(settings.audit_log_path)
    policy = PolicyStore(session_factory, audit=audit)
    memory = app.build_memory(settings)
    return policy, audit, memory


def test_build_google_services_includes_gmail_only_when_enabled() -> None:
    def names(s: Settings) -> set[str]:
        return {svc.name for svc in app.build_google_services(s)}

    # After the issue #52 cutover the single Gmail service is the chief-owned one
    # (name "gmail_chief"); the third-party "gmail" service was dropped.
    assert "gmail_chief" not in names(_settings(gmail_enabled=False))  # off → excluded
    assert "gmail_chief" in names(_settings(gmail_enabled=True))
    # The gmail service carries its configured URL through to the SDK config.
    (gmail,) = [
        svc
        for svc in app.build_google_services(_settings(gmail_enabled=True))
        if svc.name == "gmail_chief"
    ]
    assert gmail.server_config()["url"] == "http://127.0.0.1:8004/mcp"


def test_build_google_services_includes_browser_only_when_playwright_enabled() -> None:
    # Browser (playwright) follows the same enable-flag pattern as Google services.
    def names(s: Settings) -> set[str]:
        return {svc.name for svc in app.build_google_services(s)}

    assert "browser" not in names(_settings(playwright_enabled=False))
    assert "browser" in names(_settings(playwright_enabled=True))
    # The service carries the configured URL and the correct server_name.
    (browser,) = [
        svc
        for svc in app.build_google_services(_settings(playwright_enabled=True))
        if svc.name == "browser"
    ]
    assert browser.server_name == "playwright"
    assert browser.server_config()["url"] == "http://127.0.0.1:3000/mcp"


def test_build_telegram_stack_wires_gate_into_engine_and_adapter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings()
    policy, audit, memory = _shared(settings, session_factory)

    manager, adapter, approvals = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    assert isinstance(adapter, TelegramAdapter)
    assert adapter._engine is manager
    assert adapter._owner_id == 42
    assert manager._platform == "telegram"
    assert manager._classifier_model == "claude-haiku-4-5"
    # The shared gate + memory thread through engine, adapter, and approval manager.
    assert manager._policy is policy
    assert manager._approvals is approvals
    assert adapter._approvals is approvals
    assert manager._memory is memory
    assert adapter._memory is memory


def test_guest_params_thread_into_engine_and_adapter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(
        guest_enabled=True,
        front_desk_thread_key="-100:9",
        calendar_enabled=True,
        guest_rate_per_window=4,
    )
    policy, audit, memory = _shared(settings, session_factory)

    manager, adapter, _ = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    # Engine: guest model + narrowed calendar + the owner admin tool are wired.
    assert manager._guest_model == settings.guest_model
    assert manager._guest_calendar_service is not None
    assert manager._guest_admin_service is not None
    # Adapter: guests on, Front Desk + rate config + the IO threaded through.
    assert isinstance(adapter, TelegramAdapter)
    assert adapter._guest_enabled is True
    assert adapter._front_desk_thread_key == "-100:9"
    assert adapter._guest_rate == 4
    assert adapter._io is not None


def test_guest_calendar_absent_when_calendar_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(
        guest_enabled=True, front_desk_thread_key="-100:9", calendar_enabled=False
    )
    policy, audit, memory = _shared(settings, session_factory)

    manager, _, _ = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    # Guests on but calendar off → take-a-message only (degraded mode).
    assert manager._guest_calendar_service is None
    assert manager._guest_admin_service is not None


def test_build_discord_stack_wires_gate_into_engine_and_adapter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(owner_discord_id=99, discord_bot_token="dc")
    policy, audit, memory = _shared(settings, session_factory)

    manager, adapter, approvals = app.build_discord_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    assert isinstance(adapter, DiscordAdapter)
    assert adapter._engine is manager
    assert adapter._owner_id == 99
    assert manager._platform == "discord"
    assert manager._policy is policy
    assert manager._approvals is approvals
    assert adapter._approvals is approvals
    assert manager._memory is memory
    assert adapter._memory is memory


def test_build_stacks_selects_configured_platforms(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(owner_discord_id=99, discord_bot_token="dc")
    policy, audit, memory = _shared(settings, session_factory)

    def platforms(s: Settings) -> set[str]:
        stacks = app.build_stacks(
            s,
            session_factory=session_factory,
            policy=policy,
            audit=audit,
            memory=memory,
        )
        return {manager._platform for manager, _adapter, _approvals in stacks}

    assert platforms(settings) == {"telegram", "discord"}
    assert platforms(_settings()) == {"telegram"}  # discord not configured
    discord_only = _settings(
        owner_telegram_id=0, telegram_bot_token=None, owner_discord_id=99,
        discord_bot_token="dc",
    )
    assert platforms(discord_only) == {"discord"}


def test_build_engine_wires_schedule_services_when_enabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(scheduler_enabled=True, primary_thread_key="-100:1")
    policy, audit, memory = _shared(settings, session_factory)

    manager, _, _ = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    # Enabled → both the benign + gated schedule services thread into the engine.
    assert manager._schedule_service is not None
    assert manager._schedule_bash_service is not None


def test_build_engine_no_schedule_services_when_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings()  # scheduler_enabled defaults off
    policy, audit, memory = _shared(settings, session_factory)

    manager, _, _ = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    assert manager._schedule_service is None
    assert manager._schedule_bash_service is None


def test_build_engine_wires_skills_when_enabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(skills_enabled=True)
    policy, audit, memory = _shared(settings, session_factory)

    manager, _, _ = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    # Enabled → flag + curated list thread in, and the plugin path is ABSOLUTE (the
    # owner session's cwd=memory_dir makes a relative --plugin-dir resolve wrong).
    assert manager._skills_enabled is True
    assert manager._default_skills == settings.default_skills
    assert manager._skills_plugin_path is not None
    assert os.path.isabs(manager._skills_plugin_path)
    assert manager._skills_plugin_path.endswith(os.path.join("vendor", "chief-skills"))


def test_build_engine_no_skills_when_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings()  # skills_enabled defaults off
    policy, audit, memory = _shared(settings, session_factory)

    manager, _, _ = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    assert manager._skills_enabled is False
    assert manager._skills_plugin_path is None


def test_build_engine_wires_budget_when_enabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(
        budget_enabled=True,
        primary_thread_key="-100:1",
        premium_request_cap=150,
        openrouter_dollar_cap=50.0,
    )
    policy, audit, memory = _shared(settings, session_factory)

    manager, _, _ = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    # The gate is built against this platform's IO, routes to the owner inbox, and
    # carries a per-currency policy for each configured cap; the downgrade model + inbox
    # thread into the engine (#84).
    assert isinstance(manager._budget, BudgetGate)
    assert isinstance(manager._budget._io, TelegramTaskIO)  # this platform's IO
    assert manager._budget._owner_inbox == "-100:1"
    assert manager._budget._policies[usage.PREMIUM_REQUESTS].cap == 150.0
    assert manager._budget._policies[usage.OPENROUTER_DOLLARS].cap == 50.0
    assert manager._owner_inbox == "-100:1"
    assert manager._budget_downgrade_model == settings.budget_downgrade_model


def test_build_discord_stack_wires_budget_when_enabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The Discord builder got the same build_budget line — assert its twin so a
    # copy-paste regression (wrong io / dropped arg) can't slip through uncaught.
    settings = _settings(
        owner_discord_id=99,
        discord_bot_token="dc",
        budget_enabled=True,
        primary_thread_key="-100:1",
    )
    policy, audit, memory = _shared(settings, session_factory)

    manager, _, _ = app.build_discord_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    assert isinstance(manager._budget, BudgetGate)
    assert isinstance(manager._budget._io, DiscordTaskIO)  # this platform's IO
    assert manager._budget._owner_inbox == "-100:1"


def test_build_engine_no_budget_when_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings()  # budget_enabled defaults off
    policy, audit, memory = _shared(settings, session_factory)

    manager, _, _ = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    assert manager._budget is None
    assert manager._owner_inbox is None


def test_build_engine_wires_versioner_from_memory(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # memory_git=True → build_memory returns a MarkdownMemory with a real GitVersioner.
    # build_engine must pass that exact instance into TaskManager so auto-commit (#22)
    # fires through the real git backend — not through the NullVersioner fallback.
    settings = _settings(memory_git=True, memory_dir=str(tmp_path / "mem"))
    policy, audit, memory = _shared(settings, session_factory)

    manager, _, _ = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    # The versioner on the manager is the SAME object the store holds — shared instance.
    assert manager._versioner is memory.versioner
    assert isinstance(manager._versioner, GitVersioner)
    # Sanity-check the inverse: memory_git=False gives a NullVersioner on both.
    settings_null = _settings(memory_git=False)
    _, audit2, memory_null = _shared(settings_null, session_factory)
    policy2 = PolicyStore(session_factory, audit=audit2)
    manager2, _, _ = app.build_telegram_stack(
        settings_null,
        session_factory=session_factory,
        policy=policy2,
        audit=audit2,
        memory=memory_null,
    )
    assert manager2._versioner is memory_null.versioner
    assert isinstance(manager2._versioner, NullVersioner)


def test_group_params_thread_into_telegram_engine_and_adapter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(
        group_chat_enabled=True,
        primary_thread_key="42:0",
        owner_home_chat_id=-100,
        group_context_max_messages=12,
    )
    policy, audit, memory = _shared(settings, session_factory)

    manager, adapter, _ = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    assert isinstance(adapter, TelegramAdapter)
    assert adapter._group_chat_enabled is True
    assert adapter._owner_home_chat_id == -100
    assert manager._group_context_max == 12
    # owner_inbox is wired from primary_thread_key for groups even with budget off, so
    # an owner-in-group approval has a private DM route (fails closed otherwise).
    assert manager._budget is None
    assert manager._owner_inbox == "42:0"


def test_group_params_thread_into_discord_engine_and_adapter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(
        owner_discord_id=99,
        discord_bot_token="dc",
        group_chat_enabled=True,
        primary_thread_key="42:0",
        owner_home_guild_id=1234,
    )
    policy, audit, memory = _shared(settings, session_factory)

    manager, adapter, _ = app.build_discord_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    assert isinstance(adapter, DiscordAdapter)
    assert adapter._group_chat_enabled is True
    assert adapter._owner_home_guild_id == 1234
    assert manager._owner_inbox == "42:0"
    assert manager._budget_downgrade_model is None


def test_build_budget_requires_primary_thread_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # budget_enabled without a primary_thread_key has nowhere to route warnings or
    # the choice card; there is no config validator, so build asserts loud (not silent).
    settings = _settings(budget_enabled=True)  # primary_thread_key defaults None
    policy, audit, memory = _shared(settings, session_factory)

    with pytest.raises(AssertionError, match="primary_thread_key"):
        app.build_telegram_stack(
            settings,
            session_factory=session_factory,
            policy=policy,
            audit=audit,
            memory=memory,
        )


async def test_build_scheduler_binds_to_primary_platform_stack(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(
        owner_discord_id=99,
        discord_bot_token="dc",
        scheduler_enabled=True,
        primary_platform="discord",
        primary_thread_key="-100:1",
        heartbeat_url="https://hc.example/ping",
    )
    policy, audit, memory = _shared(settings, session_factory)
    stacks = app.build_stacks(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )
    (discord,) = [s for s in stacks if s[0].platform == "discord"]

    scheduler, http = app.build_scheduler(
        settings, stacks=stacks, session_factory=session_factory
    )

    assert scheduler is not None
    # The single loop binds to the primary platform's manager (its IO + waker).
    assert scheduler._io is discord[0].io
    assert scheduler._waker is discord[0]
    assert scheduler._primary_thread_key == "-100:1"
    # A heartbeat_url → a real http client is built + handed in for the dead-man switch.
    assert http is not None
    assert scheduler._http is http
    await http.aclose()


async def test_build_scheduler_no_http_without_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(scheduler_enabled=True, primary_thread_key="-100:1")
    policy, audit, memory = _shared(settings, session_factory)
    stacks = app.build_stacks(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    scheduler, http = app.build_scheduler(
        settings, stacks=stacks, session_factory=session_factory
    )

    assert scheduler is not None
    assert http is None
    assert scheduler._http is None


def test_build_scheduler_none_when_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings()  # scheduler_enabled defaults off
    policy, audit, memory = _shared(settings, session_factory)
    stacks = app.build_stacks(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    scheduler, http = app.build_scheduler(
        settings, stacks=stacks, session_factory=session_factory
    )

    assert scheduler is None and http is None


async def test_seed_on_boot_populates_policy(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    settings = _settings(
        never_seed=[PolicySeed(tool="WebFetch")],
        approved_seed=[PolicySeed(tool="Bash", arg_pattern="git status")],
        audit_log_path=str(tmp_path / "audit.jsonl"),
    )
    audit = AuditLog(settings.audit_log_path)
    policy = PolicyStore(session_factory, audit=audit)

    # Mirror serve()'s seeding step.
    await policy.seed(
        never=[s.as_pair() for s in settings.never_seed],
        approved=[s.as_pair() for s in settings.approved_seed],
    )

    assert policy.classify_against("Bash", {"command": "git status"}) == (
        policy_repo.APPROVED
    )
    assert policy.classify_against("WebFetch", {"url": "http://x"}) == (
        policy_repo.NEVER
    )


async def test_re_arm_on_boot_recovers_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        approval = await appr_repo.create_approval(
            session, task_id=None, kind="Bash", payload_preview="Run: git push"
        )
        await appr_repo.set_state(session, approval, appr_repo.NOTIFIED)
        approval_id = approval.id
    settings = _settings()
    policy, audit, memory = _shared(settings, session_factory)
    _, _, approvals = app.build_telegram_stack(
        settings,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )

    rearmed = await approvals.re_arm()

    assert rearmed == [approval_id]
