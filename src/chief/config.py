"""Typed application settings, split non-secret config from secrets.

Non-secret values come from ``config.yaml`` (committed) with environment-variable
overrides; secrets (Telegram + Claude OAuth tokens) come from a Docker ``secrets_dir``
(``/run/secrets``) with an environment fallback for local runs. Precedence, highest
first: explicit init kwargs → environment → ``config.yaml`` → secret files.
"""

import os
from typing import Any

from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


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
