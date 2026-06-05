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


def test_m3_gate_defaults_and_seed_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\n"
        "approval_timeout_seconds: 120\n"
        "never_seed:\n"
        "  - {tool: WebFetch}\n"
        "approved_seed:\n"
        "  - {tool: Bash, arg_pattern: git status}\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.approval_timeout_seconds == 120
    assert settings.front_desk_thread_key is None  # stub default
    assert settings.never_seed[0].as_pair() == ("WebFetch", None)
    assert settings.approved_seed[0].as_pair() == ("Bash", "git status")


def test_guest_enabled_requires_front_desk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guests on, but no Front Desk thread for their approvals/relays to land in.
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\nguest_enabled: true\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="front_desk_thread_key"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_guest_enabled_with_front_desk_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\n"
        "guest_enabled: true\n"
        'front_desk_thread_key: "-100:9"\n'
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.guest_enabled
    assert settings.front_desk_thread_key == "-100:9"
    assert settings.guest_rate_per_window == 10
    assert settings.guest_global_rate_per_window == 60


def test_no_platform_configured_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Token present but no owner id → Telegram half-set, Discord absent: rejected.
    (tmp_path / "config.yaml").write_text("owner_name: Will\n")  # no owner_telegram_id
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="no chat platform configured"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_telegram_only_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 42\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.telegram_configured
    assert not settings.discord_configured


def test_discord_only_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No Telegram at all; Discord owner id + token alone is a valid deploy.
    (tmp_path / "config.yaml").write_text("owner_discord_id: 99\n")
    secrets = tmp_path / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "discord_bot_token").write_text("dc-secret")
    (secrets / "claude_code_oauth_token").write_text("oauth-secret")
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.discord_configured
    assert not settings.telegram_configured
    assert settings.discord_bot_token == "dc-secret"


def test_both_platforms_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 42\nowner_discord_id: 99\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    (secrets / "discord_bot_token").write_text("dc-secret")
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.telegram_configured
    assert settings.discord_configured


def test_zero_owner_id_is_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # config.yaml ships owner_telegram_id: 0 as the unset sentinel — must not count.
    yaml = "owner_telegram_id: 0\nowner_discord_id: 99\n"
    (tmp_path / "config.yaml").write_text(yaml)
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    (secrets / "discord_bot_token").write_text("dc-secret")
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert not settings.telegram_configured  # id 0 → not configured
    assert settings.discord_configured


def test_blank_owner_id_env_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # docker-compose passes ${OWNER_TELEGRAM_ID:-} as an *empty string* when the
    # deployer leaves it unset. That must read as "platform off", not a parse error,
    # so the Discord-only deploy boots.
    (tmp_path / "config.yaml").write_text("owner_discord_id: 99\n")
    secrets = tmp_path / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "discord_bot_token").write_text("dc-secret")
    (secrets / "claude_code_oauth_token").write_text("oauth-secret")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "")

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.owner_telegram_id is None
    assert not settings.telegram_configured
    assert settings.discord_configured
