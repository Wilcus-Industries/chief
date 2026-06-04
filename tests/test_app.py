"""Entrypoint settings loading and component wiring."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram.ext import Application

from chief import app
from chief.config import PolicySeed, Settings
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
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _application() -> Application:  # type: ignore[type-arg]
    return cast(
        Application,  # type: ignore[type-arg]
        SimpleNamespace(bot=object(), add_handler=Mock()),
    )


def test_build_components_wires_gate_into_engine_and_adapter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager, adapter, policy, approvals, memory = app.build_components(
        _settings(memory_git=False),
        application=_application(),
        session_factory=session_factory,
    )

    assert adapter._engine is manager
    assert adapter._owner_id == 42
    assert manager._classifier_model == "claude-haiku-4-5"
    # The gate is shared across engine, adapter, and the approval manager.
    assert manager._policy is policy
    assert manager._approvals is approvals
    assert adapter._approvals is approvals
    # The one memory store is shared by the engine (distill/recall) and the adapter
    # (/memory, /forget).
    assert manager._memory is memory
    assert adapter._memory is memory


async def test_seed_on_boot_populates_policy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings(
        never_seed=[PolicySeed(tool="WebFetch")],
        approved_seed=[PolicySeed(tool="Bash", arg_pattern="git status")],
    )
    _, _, policy, _, _ = app.build_components(
        settings, application=_application(), session_factory=session_factory
    )

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
    _, _, _, approvals, _ = app.build_components(
        _settings(memory_git=False),
        application=_application(),
        session_factory=session_factory,
    )

    rearmed = await approvals.re_arm()

    assert rearmed == [approval_id]
