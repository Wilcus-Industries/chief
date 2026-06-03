"""Entrypoint settings loading."""

from pathlib import Path

import pytest

from chief import app


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
