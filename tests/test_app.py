"""Entrypoint settings loading and per-platform component wiring."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief import app
from chief.adapters.discord import DiscordAdapter
from chief.adapters.telegram import TelegramAdapter
from chief.config import PolicySeed, Settings
from chief.gate.policy import PolicyStore
from chief.memory.store import MemoryStore
from chief.obs.audit import AuditLog
from chief.persistence import approvals as appr_repo
from chief.persistence import policy as policy_repo


def test_load_settings_uses_env_when_no_secrets_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 5\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-tg")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-oauth")
    monkeypatch.setattr(app, "DOCKER_SECRETS_DIR", str(tmp_path / "absent"))

    settings = app.load_settings()

    assert settings.owner_telegram_id == 5
    assert settings.telegram_bot_token == "env-tg"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        owner_telegram_id=42,
        telegram_bot_token="x:y",
        claude_code_oauth_token="t",
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
