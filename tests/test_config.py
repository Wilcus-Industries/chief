"""Settings load from yaml + env + secrets_dir, and the API-key guard."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from chief.config import Settings


def _write_secrets(secrets_dir: Path) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "telegram_bot_token").write_text("tg-secret")
    (secrets_dir / "claude_code_oauth_token").write_text("oauth-secret")


def test_loads_from_yaml_and_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 42\nowner_name: Will\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.owner_telegram_id == 42
    assert settings.owner_name == "Will"
    assert settings.telegram_bot_token == "tg-secret"
    assert settings.claude_code_oauth_token == "oauth-secret"
    # Unset field falls back to its default.
    assert settings.owner_model_default == "claude-sonnet-4-6"


def test_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "999")

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.owner_telegram_id == 999


def test_env_provides_secrets_without_secrets_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 7\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-tg")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-oauth")
    empty_secrets = tmp_path / "empty"
    empty_secrets.mkdir()

    settings = Settings(_secrets_dir=str(empty_secrets))  # type: ignore[call-arg]

    assert settings.telegram_bot_token == "env-tg"
    assert settings.claude_code_oauth_token == "env-oauth"


def test_rejects_anthropic_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-here")

    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_m2_defaults_and_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONCURRENCY", "5")

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.concurrency == 5  # env override
    assert settings.grace_seconds == 6.0
    assert settings.idle_archive_seconds == 3600
    assert settings.classifier_model == "claude-haiku-4-5"


def test_missing_required_field_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("owner_name: Will\n")  # no owner_telegram_id
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="owner_telegram_id"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]
