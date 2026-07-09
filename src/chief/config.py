"""Typed application settings, split non-secret config from secrets.

Non-secret values come from ``config.yaml`` (committed) with environment-variable
overrides; secrets (the per-platform bot tokens + the Claude OAuth token) come from a
``secrets_dir`` of one-file-per-secret (``~/.config/chief/secrets`` or the repo-local
``./secrets`` — see :func:`chief.app.load_settings`) with an environment fallback.
Precedence, highest first: explicit init kwargs → environment → ``config.yaml`` → secret
files. At least one chat platform (Telegram and/or Discord) must be fully configured.
"""

import os
import re
from datetime import time
from typing import Any

from pydantic import BaseModel, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from .gate.blacklist import DEFAULT_SHELL_PATTERNS
from .tools.calendar.mcp import WRITE_TOOLS as _CALENDAR_WRITE_TOOLS
from .tools.drive.mcp import WRITE_TOOLS as _DRIVE_WRITE_TOOLS
from .tools.gmail.mcp import WRITE_TOOLS as _GMAIL_WRITE_TOOLS
from .tools.sheets.mcp import WRITE_TOOLS as _SHEETS_WRITE_TOOLS

#: Default ``blacklist_tools`` seed (MEDIUM-1 fix): under the owner's default-allow
#: gate, being absent from ``allowed_tools`` no longer routes a call to an approval
#: card by itself — ``classify()`` (chief.gate.gate) ALLOWs anything not APPROVED or
#: blacklisted. Every Google write tool (Gmail send/reply/draft/label/trash, calendar
#: create/update, Drive upload, Sheets writes) is seeded here so the "reads ALLOW,
#: writes ASK" design each service's ``tools/<svc>/mcp.py`` module documents actually
#: holds under the new posture, instead of running silently uncarded.
_DEFAULT_BLACKLIST_TOOLS: tuple[str, ...] = (
    _GMAIL_WRITE_TOOLS
    + _CALENDAR_WRITE_TOOLS
    + _DRIVE_WRITE_TOOLS
    + _SHEETS_WRITE_TOOLS
)


class PolicySeed(BaseModel):
    """One boot-seeded permission rule. ``arg_pattern`` ``None`` = whole-tool rule."""

    tool: str
    arg_pattern: str | None = None

    def as_pair(self) -> tuple[str, str | None]:
        """The ``(tool, arg_pattern)`` tuple :meth:`PolicyStore.seed` expects."""
        return self.tool, self.arg_pattern


class RouteSeed(BaseModel):
    """One boot-seeded routing row: a job category → ``{target_class, model}`` target
    (#79). ``target_class`` is ``copilot`` (Copilot quota, e.g. model ``auto``) or
    ``openrouter`` (a BYOK per-model target)."""

    category: str
    target_class: str
    model: str

    def as_tuple(self) -> tuple[str, str, str]:
        """The ``(category, target_class, model)`` tuple :meth:`RoutingStore.seed`
        expects."""
        return self.category, self.target_class, self.model


class Settings(BaseSettings):
    """Validated configuration for the chief core process."""

    # secrets_dir is enabled only when one of the candidate dirs exists
    # (app.load_settings); otherwise tokens come from env (no missing-dir warning).
    model_config = SettingsConfigDict(
        yaml_file="config.yaml",
        extra="ignore",
        protected_namespaces=(),
        # Compose forwards per-deploy overrides as ${VAR:-}, which expands to an empty
        # string when unset in .env. Ignore empties so an unset override falls through
        # to the config.yaml default instead of clobbering it (e.g. SCHEDULER_ENABLED=""
        # parsing as a bool, or PRIMARY_PLATFORM="" blanking the default).
        env_ignore_empty=True,
    )

    # Non-secret config (config.yaml / env). Each platform's owner id is optional —
    # configure Telegram, Discord, or both (the after-validator requires at least one).
    # 0 is the "unset" sentinel committed in config.yaml, treated as not configured.
    owner_telegram_id: int | None = None
    owner_discord_id: int | None = None
    owner_name: str = "the owner"
    # Owner model posture (M11). The owner defaults to owner_model_default (Sonnet) and
    # reaches owner_model_opus (Opus) only with approval — Opus burns the Max budget
    # faster, so it never runs unsanctioned. /opus escalates now and persists per-task
    # (Task.model); /sonnet reverts. opus_auto_detect (opt-in) additionally lets a
    # per-turn complexity classifier ASK to escalate a complex owner task; the command
    # works regardless. Guests are pinned to guest_model (never Opus).
    owner_model_default: str = "claude-sonnet-4-6"
    owner_model_opus: str = "claude-opus-4-8"
    opus_auto_detect: bool = False
    # Model routing (#79, part of #72), default off (mirror the opt-in subsystem
    # pattern). When routing_enabled, an owner task's spawning message is
    # auto-classified into one job category (routing_seed) on the cheap classifier_model
    # — never a routing target — and the category's target picks the owner session's
    # model + BYOK provider: a ``copilot`` target runs on Copilot quota (model ``auto``,
    # since the Student plan exposes no model choice — spike #74), an ``openrouter``
    # target is a BYOK per-model call (needs openrouter_api_key). /route overrides a
    # task; routing_surface_defaults (keyed by Surface value home/dm/group) pins a
    # category per surface without classifying. The category set is persisted as data —
    # exactly the seeded routes. Inert when disabled.
    routing_enabled: bool = False
    routing_seed: list[RouteSeed] = [
        RouteSeed(category="writing", target_class="copilot", model="auto"),
        RouteSeed(category="research", target_class="copilot", model="auto"),
        RouteSeed(category="general", target_class="copilot", model="auto"),
        RouteSeed(
            category="code",
            target_class="openrouter",
            model="deepseek/deepseek-v4-flash",
        ),
        RouteSeed(
            category="reasoning",
            target_class="openrouter",
            model="deepseek/deepseek-v4-flash",
        ),
    ]
    routing_surface_defaults: dict[str, str] = {}
    guest_model: str = "claude-sonnet-4-6"
    # Agent backend seam (#75, part of #72 — the strangler scaffold). Every session the
    # engine drives is built through an AgentBackend; ``claude`` (claude-agent-sdk) is
    # the incumbent and ``copilot`` (the GitHub Copilot SDK, #76) is the alternative.
    # Config-selectable so the harness swap is a one-line change per deployment.
    agent_backend: str = "claude"
    db_path: str = "data/chief.db"
    guest_ack: str = (
        "Thanks for reaching out — I'm an assistant and I've passed your message along."
    )

    # Task engine (M2). concurrency bounds turns actively generating;
    # turn_timeout_seconds is the per-turn watchdog (a turn whose stream never
    # terminates is torn down, not left to freeze the task); idle_archive_seconds is the
    # no-activity → archive timer for real task threads; compaction_idle_seconds is the
    # casual-channel twin — instead of archiving (a ``:0`` channel has no closable
    # topic) it self-compacts at this idle window; classifier_model runs the cheap
    # stop-intent / warrants-a-task judgments.
    concurrency: int = 3
    turn_timeout_seconds: float = 300.0
    idle_archive_seconds: int = 3600
    compaction_idle_seconds: float = 3600.0
    classifier_model: str = "claude-haiku-4-5"

    # Permission gate + approval flow (M3, flipped to default-allow for the owner in
    # the host-native rework). approval_timeout_seconds is the fail-closed deny window;
    # never_seed/approved_seed prime the NEVER/APPROVED lists on boot; audit_log_path
    # is the append-only JSONL sink; front_desk_thread_key is the thread
    # guest-originated approvals, admission cards, and relayed messages post to (M6).
    # blacklist_shell_patterns are regexes over shell commands (and blacklist_tools
    # whole tool names) that still raise an approval card under the owner's
    # default-allow posture; guests stay default-ask regardless. Being absent from a
    # session's ``allowed_tools`` is NOT enough on its own to card an owner call
    # anymore — ``classify()`` (chief.gate.gate) ALLOWs anything not APPROVED or on
    # one of these two blacklists, so blacklist_tools is what actually restores
    # "writes ASK" for the Google services (default-seeded — see
    # ``_DEFAULT_BLACKLIST_TOOLS`` above).
    approval_timeout_seconds: float = 600.0
    never_seed: list[PolicySeed] = []
    approved_seed: list[PolicySeed] = []
    audit_log_path: str = "data/audit.jsonl"
    front_desk_thread_key: str | None = None
    blacklist_shell_patterns: tuple[str, ...] = DEFAULT_SHELL_PATTERNS
    blacklist_tools: tuple[str, ...] = _DEFAULT_BLACKLIST_TOOLS

    # Untrusted-content screening (host-native security seam). Material arriving from
    # the internet (screening_tools results) or from guests (the Front Desk relay) is
    # screened by a cheap screening_model call for prompt injection before the owner
    # agent acts on it; a hit is annotated with a warning (screening_block=true blocks
    # the result outright instead). Fail-safe: a screener error passes content through.
    # screening_block defaults False, i.e. screening is advisory/annotate-only — it
    # never itself blocks a turn unless flipped on (MEDIUM/LOW finding).
    screening_enabled: bool = True
    screening_model: str = "claude-haiku-4-5"
    screening_block: bool = False
    # MEDIUM-2 fix: Gmail reads (email bodies are attacker-controlled), the Drive read,
    # and the playwright browser action tools that return an updated page
    # snapshot/result alongside the interaction (click/type/hover/drag/drop/
    # select_option/press_key/fill_form/file_upload/handle_dialog) are untrusted
    # channels too, not just the original fetch/search/navigate/snapshot set.
    screening_tools: tuple[str, ...] = (
        "WebFetch",
        "WebSearch",
        "mcp__playwright__browser_snapshot",
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_navigate_back",
        "mcp__playwright__browser_click",
        "mcp__playwright__browser_type",
        "mcp__playwright__browser_hover",
        "mcp__playwright__browser_drag",
        "mcp__playwright__browser_drop",
        "mcp__playwright__browser_select_option",
        "mcp__playwright__browser_press_key",
        "mcp__playwright__browser_fill_form",
        "mcp__playwright__browser_file_upload",
        "mcp__playwright__browser_handle_dialog",
        "mcp__gmail_chief__gmail_list_messages",
        "mcp__gmail_chief__gmail_get_message",
        "mcp__gmail_chief__gmail_search_messages",
        "mcp__gmail_chief__gmail_list_drafts",
        "mcp__drive__ReadDriveFile",
    )

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

    # Long-term memory (M4). memory_dir holds Soul/User + facts/ (a host directory,
    # gitignored); memory_git versions every write op via subprocess git under the
    # configured author identity.
    memory_dir: str = "data/memory"
    memory_git: bool = True
    git_author_name: str = "chief"
    git_author_email: str = "chief@localhost"

    # Google services (M5/M8). Each enabled server wires its own MCP container over
    # streamable HTTP at <svc>_mcp_url into owner sessions — the containers publish
    # their ports on 127.0.0.1 so the host-native core reaches them via localhost:
    # reads ALLOWed, writes approval-gated, deferred ops blocked. One shared OAuth
    # token covers all four (see secrets/README.md). owner_tz frames calendar booking
    # times (an IANA
    # name, e.g. America/New_York). The gmail container's transparent signature is not a
    # Settings field: it reads the GMAIL_SIGNATURE compose env at its own startup (see
    # secrets/README.md). Each defaults off until its token + container exist.
    calendar_enabled: bool = False
    calendar_mcp_url: str = "http://127.0.0.1:8003/mcp"
    drive_enabled: bool = False
    drive_mcp_url: str = "http://127.0.0.1:8001/mcp"
    sheets_enabled: bool = False
    sheets_mcp_url: str = "http://127.0.0.1:8002/mcp"
    gmail_enabled: bool = False
    # Cutover (issue #52): the mcp-gmail service now runs the chief-owned server on
    # :8004 (the third-party mcp-google-gmail dependency was dropped). The SDK server
    # name stays ``gmail_chief`` so the per-thread account rebuild keeps matching.
    gmail_mcp_url: str = "http://127.0.0.1:8004/mcp"
    owner_tz: str = "UTC"

    # Browser automation (M13+), owner-only, default off (mirror the opt-in pattern).
    # playwright_enabled wires the stock @playwright/mcp 0.0.76 container (headless
    # Chromium, streamable HTTP at playwright_mcp_url) into owner sessions: read tools
    # (navigate, snapshot, screenshot, inspection) ALLOWed, write tools (click, type,
    # JS evaluation) approval-gated. Guest sessions never see browser tools.
    playwright_enabled: bool = False
    playwright_mcp_url: str = "http://127.0.0.1:3000/mcp"
    # Host directory bind-mounted into mcp-playwright at /screenshots. Core reads
    # screenshot files from here to deliver them via send_file after
    # browser_take_screenshot runs.
    playwright_screenshots_dir: str = "data/screenshots"

    # Host shell + file workspace (M7, host-native rework), owner-only.
    # shell_enabled wires the in-process bash tool that runs a persistent per-task
    # shell directly on the host ($SHELL, else bash, else zsh) with the full process
    # environment; commands run freely unless they match the approval blacklist.
    # workspace_enabled adds Write/Edit to the pre-approved tool list; workspace_dir is
    # the shell's starting cwd and the suggested scratch area (writes are no longer
    # confined to it). shell_timeout_seconds bounds one command (a hung shell is
    # SIGINT'd then respawned); shell_output_limit caps captured bytes.
    shell_enabled: bool = False
    workspace_enabled: bool = False
    workspace_dir: str = "data/workspace"
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

    # Group chats (M11), default off (mirror the opt-in subsystem pattern). A GROUP is
    # any multi-party chat chief is invited to that ISN'T the owner's own HOME surface —
    # owner_home_chat_id (Telegram) / owner_home_guild_id (Discord) name HOME so the
    # adapters can tell it from a GROUP. In a group chief reads every message ambiently
    # but answers only when @mentioned/replied-to; owner-engaged tool approvals are DM'd
    # to primary_thread_key (never shown in the group), so the after-validator requires
    # it plus at least one home id. group_context_max_messages caps the per-group
    # ambient buffer (last N since join) so the shared transcript stays bounded.
    group_chat_enabled: bool = False
    owner_home_chat_id: int | None = None
    owner_home_guild_id: int | None = None
    group_context_max_messages: int = 50

    # Secrets (secrets_dir / env). The bot tokens are per-platform and optional, paired
    # with their owner id by the configured-platform check; the OAuth token is always
    # required (it authenticates the Claude SDK regardless of chat platform).
    telegram_bot_token: str | None = None
    discord_bot_token: str | None = None
    claude_code_oauth_token: str
    # OpenRouter BYOK provider target class (#90, part of #72): the key for the SDK's
    # "openai" provider pointed at OpenRouter. Optional — only required when a session
    # is actually spawned on an ``openrouter`` target. Never the Copilot token itself,
    # which is CLI-managed in ``~/.copilot/config.json`` and never touches secrets_dir.
    openrouter_api_key: str | None = None

    @field_validator(
        "owner_telegram_id",
        "owner_discord_id",
        "owner_home_chat_id",
        "owner_home_guild_id",
        mode="before",
    )
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

    @field_validator(
        "db_path",
        "audit_log_path",
        "memory_dir",
        "workspace_dir",
        "playwright_screenshots_dir",
    )
    @classmethod
    def _expand_user_paths(cls, value: str) -> str:
        """Expand a leading ``~`` so host configs can point at home-dir paths.

        Relative paths stay relative (resolved against the process cwd — the repo
        root under the ``chief`` launcher), matching how ``config.yaml`` is found.
        """
        return os.path.expanduser(value)

    @field_validator("blacklist_shell_patterns")
    @classmethod
    def _validate_blacklist_patterns(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Each blacklist entry must be a valid regex — a typo fails the boot, not the
        first shell command (which would then run un-carded)."""
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"blacklist_shell_patterns entry {pattern!r} is not a valid "
                    f"regex: {exc}"
                ) from exc
        return value

    @field_validator("agent_backend")
    @classmethod
    def _validate_agent_backend(cls, value: str) -> str:
        """Only ``claude`` or ``copilot`` are valid backends (#75 / #76).

        Fails the boot on an unknown name rather than at first turn; the runtime
        registry (:func:`chief.core.backend.select_backend`) enforces the same rule.
        """
        valid = ("claude", "copilot")
        if value not in valid:
            raise ValueError(
                f"agent_backend must be one of {valid}, got {value!r}"
            )
        return value

    @field_validator("routing_seed")
    @classmethod
    def _validate_routing_seed(cls, value: list[RouteSeed]) -> list[RouteSeed]:
        """Each routing row needs a non-empty category/model and a known target class.

        Validated at load time so a typo (a stray target class, or a blank category the
        classifier can never emit) fails the boot rather than the first routed task.
        """
        valid = ("copilot", "openrouter")
        for seed in value:
            if seed.target_class not in valid:
                raise ValueError(
                    f"routing_seed target_class must be one of {valid}, got "
                    f"{seed.target_class!r}"
                )
            if not seed.category.strip() or not seed.model.strip():
                raise ValueError(
                    "routing_seed entries need a non-empty category and model"
                )
        return value

    @field_validator("routing_surface_defaults")
    @classmethod
    def _validate_surface_defaults(cls, value: dict[str, str]) -> dict[str, str]:
        """Each per-surface default keys off a real Surface value (home/dm/group)."""
        valid = {"home", "dm", "group"}
        for surface in value:
            if surface not in valid:
                raise ValueError(
                    f"routing_surface_defaults keys must be Surface values {valid}, "
                    f"got {surface!r}"
                )
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

    @field_validator("group_context_max_messages")
    @classmethod
    def _validate_group_buffer_cap(cls, value: int) -> int:
        """The ambient buffer cap must be positive — ``deque(maxlen=0)`` would silently
        drop every group message, so a ``0`` or negative is a config typo, not 'off'."""
        if value <= 0:
            raise ValueError(
                f"group_context_max_messages must be > 0, got {value!r}"
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
    def _require_targets_for_group_chats(self) -> "Settings":
        """Group chats need a private owner inbox for approvals and a HOME id.

        Without ``primary_thread_key`` an owner-engaged group tool's approval card has
        nowhere private to land — and it must never show in the group (M11). Without an
        ``owner_home_*`` id every chat the owner speaks in would look like HOME, so no
        chat would ever classify as a GROUP. Mirrors ``_require_front_desk_for_guests``.
        """
        if not self.group_chat_enabled:
            return self
        if not self.primary_thread_key:
            raise ValueError(
                "group_chat_enabled requires primary_thread_key — owner-engaged group "
                "approvals are DM'd there, never shown in the group."
            )
        if self.owner_home_chat_id is None and self.owner_home_guild_id is None:
            raise ValueError(
                "group_chat_enabled requires at least one of owner_home_chat_id / "
                "owner_home_guild_id to tell the owner's HOME surface from a GROUP."
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
