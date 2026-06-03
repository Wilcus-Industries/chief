"""Entrypoint settings loading and component wiring."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram.ext import Application

from chief import app
from chief.config import Settings


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


def test_build_components_wires_engine_into_adapter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        owner_telegram_id=42,
        telegram_bot_token="x:y",
        claude_code_oauth_token="t",
        classifier_model="claude-haiku-4-5",
    )
    application = cast(
        Application,  # type: ignore[type-arg]
        SimpleNamespace(bot=object(), add_handler=Mock()),
    )

    manager, adapter = app.build_components(
        settings, application=application, session_factory=session_factory
    )

    assert adapter._engine is manager
    assert adapter._owner_id == 42
    assert manager._classifier_model == "claude-haiku-4-5"
