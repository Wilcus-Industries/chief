"""Typed application settings, split non-secret config from secrets.

Non-secret values come from ``config.yaml`` (committed) with environment-variable
overrides; secrets (the per-platform bot tokens + the Claude OAuth token) come from a
Docker ``secrets_dir`` (``/run/secrets``) with an environment fallback for local runs.
Precedence, highest first: explicit init kwargs → environment → ``config.yaml`` → secret
files. At least one chat platform (Telegram and/or Discord) must be fully configured.
"""

import os
from datetime import time
from typing import Any

from pydantic import BaseModel, field_validator, model_validator
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

    # Non-secret config (config.yaml / env). Each platform's owner id is optional —
    # configure Telegram, Discord, or both (the after-validator requires at least one).
    # 0 is the "unset" sentinel committed in config.yaml, treated as not configured.
    owner_telegram_id: int | None = None
    owner_discord_id: int | None = None
    owner_name: str = "the owner"
    owner_model_default: str = "claude-sonnet-4-6"
    guest_model: str = "claude-sonnet-4-6"
    db_path: str = "chief.db"
    guest_ack: str = (
        "Thanks for reaching out — I'm an assistant and I've passed your message along."
    )

    # Task engine (M2). concurrency bounds turns actively generating; grace_seconds is
    # the inline-vs-"working…" window; turn_timeout_seconds is the per-turn watchdog (a
    # turn whose stream never terminates is torn down, not left to freeze the task);
    # idle_archive_seconds is the no-activity → archive timer for real task threads
    # (distillation is a separate ~10-min trigger owned by M4); compaction_idle_seconds
    # is its casual-channel twin — instead of archiving (a ``:0`` channel has no
    # closable topic) the casual lane self-compacts at this idle window;
    # classifier_model runs the cheap stop-intent / warrants-a-task judgments.
    concurrency: int = 3
    grace_seconds: float = 6.0
    turn_timeout_seconds: float = 300.0
    idle_archive_seconds: int = 3600
    compaction_idle_seconds: float = 3600.0
    classifier_model: str = "claude-haiku-4-5"

    # Permission gate + approval flow (M3). approval_timeout_seconds is the fail-closed
    # deny window; never_seed/approved_seed prime the NEVER/APPROVED lists on boot;
    # audit_log_path is the append-only JSONL sink; front_desk_thread_key is the thread
    # guest-originated approvals, admission cards, and relayed messages post to (M6).
    approval_timeout_seconds: float = 600.0
    never_seed: list[PolicySeed] = []
    approved_seed: list[PolicySeed] = []
    audit_log_path: str = "/data/audit.jsonl"
    front_desk_thread_key: str | None = None

    # Guest receptionist (M6), default off. When enabled, guests route into a tight,
    # tier-isolated session (take-a-message + calendar free/busy + owner-approved
    # booking) instead of the canned ack — and front_desk_thread_key MUST be set (the
    # after-validator enforces it). The rate limits guard the owner's Max limits: each
    # guest message counts against a per-guest cap and a global guest budget over a
    # shared window (seconds). guest_model pins guests to Sonnet (never Opus).
    guest_enabled: bool = False
    guest_rate_per_window: int = 10
    guest_rate_window_seconds: int = 3600
    guest_global_rate_per_window: int = 60

    # Long-term memory (M4). memory_dir holds Soul/User/MEMORY + facts/ (a persisted
    # volume in the container); distill_idle_seconds is the quiet window before a task's
    # chatter is distilled into facts (Sonnet, distill_model). memory_git versions every
    # write op via subprocess git under the configured author identity.
    memory_dir: str = "/memory"
    distill_idle_seconds: float = 1200.0
    distill_model: str = "claude-sonnet-4-6"
    memory_git: bool = True
    git_author_name: str = "chief"
    git_author_email: str = "chief@localhost"

    # Google services (M5/M8). Each enabled server wires its own MCP container over
    # streamable HTTP at <svc>_mcp_url (MCP at /mcp) into owner sessions: reads ALLOWed,
    # writes approval-gated, deferred ops blocked. One shared OAuth token covers all
    # four (see secrets/README.md). owner_tz frames calendar booking times (an IANA
    # name, e.g. America/New_York). The gmail container's transparent signature is not a
    # Settings field: it reads the GMAIL_SIGNATURE compose env at its own startup (see
    # secrets/README.md). Each defaults off until its token + container exist.
    calendar_enabled: bool = False
    calendar_mcp_url: str = "http://mcp-calendar:8003/mcp"
    drive_enabled: bool = False
    drive_mcp_url: str = "http://mcp-drive:8001/mcp"
    sheets_enabled: bool = False
    sheets_mcp_url: str = "http://mcp-sheets:8002/mcp"
    gmail_enabled: bool = False
    gmail_mcp_url: str = "http://mcp-gmail:8004/mcp"
    owner_tz: str = "UTC"

    # Shell sandbox + file workspace (M7), owner-only, default off (mirror the Google
    # profile pattern). shell_enabled wires the in-process bash tool that forwards to
    # the secret-free sandbox container at sandbox_host:sandbox_port; every command is
    # default-ask gated. workspace_enabled adds Write/Edit, gate-confined to
    # workspace_dir (a volume shared rw with the sandbox). shell_timeout_seconds bounds
    # one command (the sandbox SIGINTs then respawns a hung shell); shell_output_limit
    # caps captured bytes.
    shell_enabled: bool = False
    workspace_enabled: bool = False
    workspace_dir: str = "/workspace"
    sandbox_host: str = "sandbox"
    sandbox_port: int = 8765
    shell_timeout_seconds: float = 120.0
    shell_output_limit: int = 64_000

    # Scheduler (M9a), default off (mirror the opt-in subsystem pattern). When enabled,
    # the long-running tick fires reminders, recurring jobs, and self-cron the owner set
    # up. primary_thread_key is the owner inbox they land in and primary_platform picks
    # which chat stack owns the loop — both required by the after-validator. owner_tz
    # (above) frames cron + quiet hours. quiet_hours_* ("HH:MM" in owner_tz) defer a
    # non-urgent fire caught overnight to quiet_hours_end; a None start disables quiet
    # hours. heartbeat_url is an optional dead-man's-switch GET, pinged every
    # heartbeat_interval_seconds; None disables it.
    scheduler_enabled: bool = False
    scheduler_tick_seconds: float = 30.0
    primary_platform: str = "telegram"
    primary_thread_key: str | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str = "07:00"
    heartbeat_url: str | None = None
    heartbeat_interval_seconds: int = 300
    # Monitors (M9b) reuse the scheduler. A monitor checks a predicate on a cron cadence
    # and fires only on a false→true flip. This floor (seconds between checks) is
    # enforced at create time so an agent monitor can't poll every tick (Haiku budget).
    monitor_min_interval_seconds: int = 300

    # Usage budgeting (M9), default off. After the June-15 billing change chief draws
    # from a fixed monthly credit, so the risk is silently blowing it early. When
    # enabled, per-turn SDK cost rolls into a month-to-date total; crossing a
    # budget_warn_fractions tier warns the owner once, and reaching
    # budget_exhaust_fraction pauses the owner's turns and posts a choice card
    # (downgrade to budget_downgrade_model / continue full-quality / approve overflow).
    # All warnings + the card route to primary_thread_key, so budget_enabled wants it
    # set (shared with the scheduler). budget_cycle_anchor_day (1–28) picks the billing
    # cycle's reset day in owner_tz. Inert when disabled — no model-validator needed.
    budget_enabled: bool = False
    monthly_credit_usd: float = 200.0
    budget_warn_fractions: tuple[float, ...] = (0.75, 0.90)
    budget_exhaust_fraction: float = 1.0
    budget_downgrade_model: str = "claude-haiku-4-5-20251001"
    budget_cycle_anchor_day: int = 1

    # Skills framework (M10), default off (mirror the opt-in subsystem pattern). The
    # Agent SDK loads packaged workflows (SKILL.md dirs) natively: chief's own plugin
    # manifest (a local plugin) provides them and a per-session skills= filter scopes
    # which are on. Owner-only by construction — guests never get plugins/skills, the
    # same tier isolation as the tool-surface split. default_skills is the curated
    # enable-list the owner session turns on (trim/extend in config.yaml); inert when
    # disabled, so no cross-dependency model-validator — only a per-entry non-empty
    # check.
    skills_enabled: bool = False
    default_skills: tuple[str, ...] = (
        "setup-morning-brief",  # chief-owned (the rest are vendored anthropics/skills)
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

    # Secrets (secrets_dir / env). The bot tokens are per-platform and optional, paired
    # with their owner id by the configured-platform check; the OAuth token is always
    # required (it authenticates the Claude SDK regardless of chat platform).
    telegram_bot_token: str | None = None
    discord_bot_token: str | None = None
    claude_code_oauth_token: str

    @field_validator("owner_telegram_id", "owner_discord_id", mode="before")
    @classmethod
    def _blank_owner_id_is_none(cls, value: Any) -> Any:
        """Treat a blank owner id as unset (``None``).

        docker-compose passes ``${OWNER_*_ID:-}`` as an *empty string* when the deployer
        leaves it unset; without this, pydantic fails to parse ``""`` as an int and the
        single-platform deploy can't boot. Whitespace-only is treated the same.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def _validate_hhmm(cls, value: str | None) -> str | None:
        """Reject a quiet-hours bound that is not a 24-hour ``"HH:MM"`` wall clock.

        Validated at load time so a typo (e.g. ``"9am"``) fails the boot rather than the
        first tick. ``None`` (start only) disables quiet hours and passes through.
        """
        if value is None:
            return None
        try:
            hour, minute = value.split(":")
            time(int(hour), int(minute))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"quiet hours must be 24-hour HH:MM, got {value!r}"
            ) from exc
        return value

    @field_validator("budget_warn_fractions")
    @classmethod
    def _validate_warn_fractions(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        """Each warn threshold must be a fraction in ``(0, 1]``; return them sorted.

        Sorting ascending lets :class:`BudgetGate` warn each tier once in turn (the
        high-water mark only ever moves up). An out-of-range tier is a config typo.
        """
        for fraction in value:
            if not 0 < fraction <= 1:
                raise ValueError(
                    f"budget warn fractions must each be in (0, 1], got {fraction!r}"
                )
        return tuple(sorted(value))

    @field_validator("monthly_credit_usd")
    @classmethod
    def _validate_credit(cls, value: float) -> float:
        """The credit ceiling must be positive — :class:`BudgetGate` divides by it."""
        if value <= 0:
            raise ValueError(f"monthly_credit_usd must be > 0, got {value!r}")
        return value

    @field_validator("budget_exhaust_fraction")
    @classmethod
    def _validate_exhaust_fraction(cls, value: float) -> float:
        """The pause+ask trigger: a fraction in ``(0, 1]`` (1.0 = full credit)."""
        if not 0 < value <= 1:
            raise ValueError(
                f"budget_exhaust_fraction must be in (0, 1], got {value!r}"
            )
        return value

    @field_validator("budget_cycle_anchor_day")
    @classmethod
    def _validate_anchor_day(cls, value: int) -> int:
        """Cap the cycle reset day at 28 so every month has it (no Feb-30 gap)."""
        if not 1 <= value <= 28:
            raise ValueError(
                f"budget_cycle_anchor_day must be 1–28, got {value!r}"
            )
        return value

    @field_validator("default_skills")
    @classmethod
    def _validate_default_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Each enabled skill name must be non-empty — it maps to a SKILL.md name/dir
        (or ``plugin:skill``); a blank entry is a config typo the SDK can't resolve."""
        for name in value:
            if not name.strip():
                raise ValueError("default_skills entries must be non-empty")
        return value

    @property
    def telegram_configured(self) -> bool:
        """True iff both the Telegram owner id and bot token are set (non-empty)."""
        return bool(self.owner_telegram_id) and bool(self.telegram_bot_token)

    @property
    def discord_configured(self) -> bool:
        """True iff both the Discord owner id and bot token are set (non-empty)."""
        return bool(self.owner_discord_id) and bool(self.discord_bot_token)

    @model_validator(mode="after")
    def _require_a_platform(self) -> "Settings":
        """Refuse to start unless at least one chat platform is fully configured.

        Each platform needs both its owner id and its bot token; a half-set platform
        (id without token, or vice versa) does not count. This replaces the old
        "Telegram is mandatory" shape now that Discord is a first-class peer.
        """
        if not self.telegram_configured and not self.discord_configured:
            raise ValueError(
                "no chat platform configured — set owner_telegram_id + "
                "telegram_bot_token and/or owner_discord_id + discord_bot_token."
            )
        return self

    @model_validator(mode="after")
    def _require_front_desk_for_guests(self) -> "Settings":
        """Guests need a Front Desk: their approvals/admissions/relays route there.

        Without it, a guest booking card or admission prompt would have nowhere to land
        (and the gate would otherwise self-route a guest approval back into the guest's
        own DM — exactly the leak M6 forbids).
        """
        if self.guest_enabled and not self.front_desk_thread_key:
            raise ValueError(
                "guest_enabled requires front_desk_thread_key — guest approvals, "
                "admission prompts, and relayed messages have nowhere to route."
            )
        return self

    @model_validator(mode="after")
    def _require_primary_for_scheduler(self) -> "Settings":
        """The scheduler needs an inbox on a live platform to deliver into.

        A reminder, wakeup, or heartbeat alert with no ``primary_thread_key`` has
        nowhere to land; and ``primary_platform`` must be a fully configured chat
        platform (owner id + token), since :mod:`chief.app` builds the loop on that one.
        """
        if not self.scheduler_enabled:
            return self
        if not self.primary_thread_key:
            raise ValueError(
                "scheduler_enabled requires primary_thread_key — reminders, wakeups, "
                "and heartbeat alerts have no inbox to land in."
            )
        configured = {
            "telegram": self.telegram_configured,
            "discord": self.discord_configured,
        }
        if not configured.get(self.primary_platform):
            raise ValueError(
                f"scheduler_enabled needs primary_platform={self.primary_platform!r} "
                "to be a fully configured chat platform (owner id + bot token)."
            )
        return self

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
