"""Typed application settings, split non-secret config from secrets.

Non-secret values come from ``config.yaml`` (committed) with environment-variable
overrides; secrets (Telegram + Claude OAuth tokens) come from a Docker ``secrets_dir``
(``/run/secrets``) with an environment fallback for local runs. Precedence, highest
first: explicit init kwargs → environment → ``config.yaml`` → secret files.
"""

import os
from typing import Any

from pydantic import BaseModel, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class PolicySeed(BaseModel):
    """One boot-seeded permission rule. ``arg_pattern`` ``None`` = whole-tool rule."""

    tool: str
    arg_pattern: str | None = None

    def as_pair(self) -> tuple[str, str | None]:
        """The ``(tool, arg_pattern)`` tuple :meth:`PolicyStore.seed` expects."""
        return self.tool, self.arg_pattern


class Settings(BaseSettings):
    """Validated configuration for the chief core process."""

    # Enable secrets_dir only when the Docker mount exists (app.load_settings).
    # Otherwise local runs read tokens from env, avoiding a missing-dir warning.
    model_config = SettingsConfigDict(
        yaml_file="config.yaml",
        extra="ignore",
        protected_namespaces=(),
    )

    # Non-secret config (config.yaml / env).
    owner_telegram_id: int
    owner_name: str = "the owner"
    owner_model_default: str = "claude-sonnet-4-6"
    guest_model: str = "claude-sonnet-4-6"
    db_path: str = "chief.db"
    guest_ack: str = (
        "Thanks for reaching out — I'm an assistant and I've passed your message along."
    )

    # Task engine (M2). concurrency bounds turns actively generating; grace_seconds is
    # the inline-vs-"working…" window; idle_archive_seconds is the no-activity → archive
    # timer (distillation is a separate ~10-min trigger owned by M4); classifier_model
    # runs the cheap stop-intent / warrants-a-task judgments.
    concurrency: int = 3
    grace_seconds: float = 6.0
    idle_archive_seconds: int = 3600
    classifier_model: str = "claude-haiku-4-5"

    # Permission gate + approval flow (M3). approval_timeout_seconds is the fail-closed
    # deny window; never_seed/approved_seed prime the NEVER/APPROVED lists on boot;
    # audit_log_path is the append-only JSONL sink; front_desk_thread_key is the M6
    # routing stub (guest-originated approvals post there once guests exist).
    approval_timeout_seconds: float = 600.0
    never_seed: list[PolicySeed] = []
    approved_seed: list[PolicySeed] = []
    audit_log_path: str = "/data/audit.jsonl"
    front_desk_thread_key: str | None = None

    # Secrets (secrets_dir / env).
    telegram_bot_token: str
    claude_code_oauth_token: str

    @model_validator(mode="before")
    @classmethod
    def _guard_anthropic_api_key(cls, data: Any) -> Any:
        """Refuse to start if ``ANTHROPIC_API_KEY`` is set.

        It outranks ``CLAUDE_CODE_OAUTH_TOKEN`` in the SDK's auth precedence, so its
        mere presence would silently bill the pay-as-you-go API instead of the Max
        subscription — the exact failure S0 exists to rule out. Runs before field
        validation so the guard fires regardless of which other fields are present.
        """
        if os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError(
                "ANTHROPIC_API_KEY is set — it outranks CLAUDE_CODE_OAUTH_TOKEN and "
                "would bill the API instead of the Max subscription. Unset it."
            )
        return data

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )
