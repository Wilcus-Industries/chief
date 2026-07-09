"""Settings load from yaml + env + secrets_dir, and the API-key guard."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.config import Settings
from chief.gate.blacklist import Blacklist
from chief.gate.gate import GateDecision, classify
from chief.gate.policy import PolicyStore


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
    assert settings.idle_archive_seconds == 3600
    assert settings.classifier_model == "claude-haiku-4-5"


def test_agent_backend_defaults_to_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The engine routes through the AgentBackend seam (#75); only "claude" is valid.
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.agent_backend == "claude"


def test_agent_backend_accepts_copilot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # copilot (the GitHub Copilot SDK backend, #76) is now a valid selection.
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\nagent_backend: copilot\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.agent_backend == "copilot"


def test_agent_backend_rejects_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # claude and copilot are the only valid backends; anything else is a config error.
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\nagent_backend: gemini\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="agent_backend"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


# ---- model routing config (#79) ---------------------------------------------


def _routing_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yaml: str
) -> Settings:
    (tmp_path / "config.yaml").write_text(yaml)
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)
    return Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_routing_defaults_off_with_the_five_seeded_categories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Routing is opt-in; the seed ships the initial 5-category table (#79).
    settings = _routing_settings(tmp_path, monkeypatch, "owner_telegram_id: 1\n")

    assert settings.routing_enabled is False
    by_category = {s.category: s for s in settings.routing_seed}
    assert set(by_category) == {
        "writing",
        "research",
        "general",
        "code",
        "reasoning",
    }
    # writing/research/general → copilot auto; code/reasoning → openrouter DeepSeek.
    assert by_category["writing"].target_class == "copilot"
    assert by_category["writing"].model == "auto"
    assert by_category["code"].target_class == "openrouter"
    assert by_category["code"].model == "deepseek/deepseek-v4-flash"


def test_routing_seed_rejects_unknown_target_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml = (
        "owner_telegram_id: 1\n"
        "routing_seed:\n"
        "  - {category: code, target_class: bedrock, model: x}\n"
    )
    with pytest.raises(ValidationError, match="target_class"):
        _routing_settings(tmp_path, monkeypatch, yaml)


def test_routing_surface_defaults_reject_unknown_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml = (
        "owner_telegram_id: 1\n"
        "routing_surface_defaults:\n"
        "  lobby: general\n"
    )
    with pytest.raises(ValidationError, match="Surface"):
        _routing_settings(tmp_path, monkeypatch, yaml)


def test_m11_opus_escalation_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner runs Sonnet by default; Opus is reachable only by escalation, and the
    # classifier-driven auto-ask is opt-in (off) so only the explicit command escalates.
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.owner_model_default == "claude-sonnet-4-6"
    assert settings.owner_model_opus == "claude-opus-4-8"
    assert settings.opus_auto_detect is False


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


def test_group_chat_enabled_requires_primary_thread_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Group chats on, with a HOME id, but no owner DM for approval cards to land in.
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\n"
        "group_chat_enabled: true\n"
        "owner_home_chat_id: -100123\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="primary_thread_key"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_group_chat_enabled_requires_a_home_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Approval target set, but no owner_home_* id to tell HOME from a GROUP.
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\n"
        "group_chat_enabled: true\n"
        'primary_thread_key: "1:0"\n'
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="owner_home"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_group_chat_enabled_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\n"
        "group_chat_enabled: true\n"
        'primary_thread_key: "1:0"\n'
        "owner_home_chat_id: -100123\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.group_chat_enabled
    assert settings.owner_home_chat_id == -100123
    assert settings.owner_home_guild_id is None
    assert settings.group_context_max_messages == 50  # default cap


def test_group_context_max_messages_must_be_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 0/negative cap is a typo: deque(maxlen=0) silently drops all ambient context.
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\ngroup_context_max_messages: 0\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="group_context_max_messages"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_group_chat_disabled_ignores_missing_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default off → no targets required, behavior identical to today (safe rollout).
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.group_chat_enabled is False
    assert settings.owner_home_chat_id is None
    assert settings.owner_home_guild_id is None


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


def test_m9_scheduler_defaults_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.scheduler_enabled is False
    assert settings.scheduler_tick_seconds == 30.0
    assert settings.primary_platform == "telegram"
    assert settings.primary_thread_key is None
    assert settings.quiet_hours_start is None
    assert settings.quiet_hours_end == "07:00"
    assert settings.heartbeat_url is None
    assert settings.heartbeat_interval_seconds == 300


def test_scheduler_enabled_requires_primary_thread_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scheduler on, but no inbox for reminders/heartbeat alerts to land in.
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\nscheduler_enabled: true\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="primary_thread_key"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_scheduler_enabled_requires_configured_primary_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # primary_platform points at Discord, but only Telegram is configured.
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\n"
        "scheduler_enabled: true\n"
        'primary_thread_key: "-100:7"\n'
        "primary_platform: discord\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="primary_platform"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_scheduler_enabled_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\n"
        "scheduler_enabled: true\n"
        'primary_thread_key: "-100:7"\n'
        "quiet_hours_start: '22:00'\n"
        "heartbeat_url: https://hc-ping.com/abc\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.scheduler_enabled
    assert settings.primary_thread_key == "-100:7"
    assert settings.primary_platform == "telegram"
    assert settings.quiet_hours_start == "22:00"
    assert settings.heartbeat_url == "https://hc-ping.com/abc"


def test_scheduler_rejects_bad_quiet_hours_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\nquiet_hours_start: 9am\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="HH:MM"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_m9_budget_defaults_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.budget_enabled is False
    assert settings.monthly_credit_usd == 200.0
    assert settings.budget_warn_fractions == (0.75, 0.90)
    assert settings.budget_exhaust_fraction == 1.0
    assert settings.budget_downgrade_model == "claude-haiku-4-5-20251001"
    assert settings.budget_cycle_anchor_day == 1


def test_budget_warn_fractions_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An out-of-order list is normalized to ascending so each tier warns in turn.
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\nbudget_warn_fractions: [0.9, 0.5, 0.75]\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.budget_warn_fractions == (0.5, 0.75, 0.9)


def test_budget_rejects_out_of_range_fraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\nbudget_warn_fractions: [0.75, 1.5]\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match=r"\(0, 1\]"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_budget_rejects_zero_exhaust_fraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\nbudget_exhaust_fraction: 0\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match=r"\(0, 1\]"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_budget_rejects_bad_anchor_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\nbudget_cycle_anchor_day: 31\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="1.*28"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_budget_rejects_nonpositive_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The BudgetGate divides spend by the credit, so a zero/negative ceiling is a
    # config error, not a degenerate "everything is over budget".
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\nmonthly_credit_usd: 0\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="monthly_credit_usd"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_m10_skills_defaults_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.skills_enabled is False
    # The curated owner default: the chief-owned workflow + Office docs + comms + dev.
    assert settings.default_skills == (
        "setup-morning-brief",
        "docx",
        "pdf",
        "pptx",
        "xlsx",
        "doc-coauthoring",
        "internal-comms",
        "claude-api",
        "skill-creator",
        "mcp-builder",
    )


def test_skills_default_skills_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner trims/extends the enable-list in config.yaml.
    (tmp_path / "config.yaml").write_text(
        "owner_telegram_id: 1\nskills_enabled: true\n"
        "default_skills: [docx, claude-api]\n"
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert settings.skills_enabled is True
    assert settings.default_skills == ("docx", "claude-api")


def test_skills_rejects_blank_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        'owner_telegram_id: 1\ndefault_skills: ["docx", ""]\n'
    )
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="non-empty"):
        Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_default_blacklist_tools_seeds_gmail_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MEDIUM-1: under owner default-allow, being absent from allowed_tools alone no
    # longer cards a Google write — blacklist_tools must seed it by default.
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert "mcp__gmail_chief__gmail_send_message" in settings.blacklist_tools
    assert "mcp__gmail_chief__gmail_reply_on_message" in settings.blacklist_tools
    # Sanity check the other Google services' writes are covered too.
    assert "mcp__calendar__create-event" in settings.blacklist_tools
    assert "mcp__drive__UploadMarkdownAsPDF" in settings.blacklist_tools
    assert "mcp__sheets__update_cells" in settings.blacklist_tools


async def test_default_blacklist_tools_card_gmail_send_for_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The seeded default actually drives classify() to ASK for the owner tier — not
    # just present in config, but wired through Blacklist.from_config correctly.
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)
    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]
    blacklist = Blacklist.from_config(
        settings.blacklist_shell_patterns, settings.blacklist_tools
    )
    policy = PolicyStore(session_factory)
    await policy.seed(never=[], approved=[])

    verdict = classify(
        "mcp__gmail_chief__gmail_send_message",
        {"to": "someone@example.com", "body": "hi"},
        policy,
        tier="owner",
        blacklist=blacklist,
    )

    assert verdict.decision is GateDecision.ASK


def test_default_screening_tools_covers_gmail_drive_and_browser_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MEDIUM-2: Gmail/Drive reads and playwright's action tools (which return an
    # updated page snapshot alongside the click/type/etc.) are untrusted channels too,
    # not just the original fetch/search/navigate/snapshot set.
    (tmp_path / "config.yaml").write_text("owner_telegram_id: 1\n")
    secrets = tmp_path / "secrets"
    _write_secrets(secrets)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]

    assert "WebFetch" in settings.screening_tools  # pre-existing default retained
    for tool in (
        "mcp__gmail_chief__gmail_list_messages",
        "mcp__gmail_chief__gmail_get_message",
        "mcp__drive__ReadDriveFile",
        "mcp__playwright__browser_click",
        "mcp__playwright__browser_type",
    ):
        assert tool in settings.screening_tools


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
