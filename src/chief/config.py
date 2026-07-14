"""Typed application settings, split non-secret config from secrets.

Non-secret values come from ``config.yaml`` (committed) with environment-variable
overrides; secrets (the per-platform bot tokens; the optional OpenRouter/Brave keys)
come from a ``secrets_dir`` of one-file-per-secret (``~/.config/chief/secrets`` or the
repo-local
``./secrets`` — see :func:`chief.app.load_settings`) with an environment fallback.
Precedence, highest first: init kwargs → environment → ``self_config.yaml`` overlay
(denylist-filtered) → ``config.yaml`` → secret files. Chat platforms (Telegram,
Discord) are optional; a chief with no tokens still boots on the always-on
client-plane socket (the ``cli`` stack).
"""

import fnmatch
import logging
import os
import re
import sys
from datetime import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from .client_plane import CLI_PLATFORM
from .gate.blacklist import DEFAULT_SHELL_PATTERNS
from .tools.apple.calendar import (
    MUTATING_TOOL_NAMES as _APPLE_CALENDAR_WRITE_TOOLS,
)
from .tools.apple.messages import READ_TOOL_NAMES as _APPLE_MESSAGES_READ_TOOLS
from .tools.apple.shortcuts import (
    MUTATING_TOOL_NAMES as _APPLE_SHORTCUTS_MUTATING_TOOLS,
)
from .tools.calendar.mcp import WRITE_TOOLS as _CALENDAR_WRITE_TOOLS
from .tools.drive.mcp import WRITE_TOOLS as _DRIVE_WRITE_TOOLS
from .tools.gmail.mcp import WRITE_TOOLS as _GMAIL_WRITE_TOOLS
from .tools.routing_admin import MUTATING_TOOL_NAMES as _ROUTING_ADMIN_TOOLS
from .tools.sheets.mcp import WRITE_TOOLS as _SHEETS_WRITE_TOOLS
from .tools.web import FETCH_TOOL_NAME as _WEB_FETCH_TOOL
from .tools.web import SEARCH_TOOL_NAME as _WEB_SEARCH_TOOL

logger = logging.getLogger("chief.config")

#: Default ``blacklist_tools`` seed (MEDIUM-1 fix): under the owner's default-allow
#: gate, being absent from ``allowed_tools`` no longer routes a call to an approval
#: card by itself — ``classify()`` (chief.gate.gate) ALLOWs anything not APPROVED or
#: blacklisted. Every Google write tool (Gmail send/reply/draft/label/trash, calendar
#: create/update, Drive upload, Sheets writes) is seeded here so the "reads ALLOW,
#: writes ASK" design each service's ``tools/<svc>/mcp.py`` module documents actually
#: holds under the new posture, instead of running silently uncarded.
#: The chief-owned ``web-fetch`` tool (#81) is seeded too: core runs host-native, so a
#: model-supplied URL is a real SSRF surface. Its ``chief.tools.web.validate_target``
#: guard denies private/loopback targets outright; this blacklist entry additionally
#: routes every fetch through an approval card (blacklist match ⇒ ASK, never DENY).
#: ``web-search`` hits a fixed trusted provider, so it is left off — it ALLOWs freely.
#: The self-config routing tool's mutating verbs (#83) are seeded too: it edits chief's
#: own routing table, so under the owner default-allow gate a bare registration would
#: run un-carded — each mutating edit must ASK. Its read-only ``list_routing`` is left
#: off (it ALLOWs freely). The gate is this tool's security boundary.
#: The Apple family's gated shapes (#155) are seeded too: ``run_shortcut`` is the
#: escape hatch to anything the owner has automated (it can send, delete, or reach
#: other people), so each shape's first run must ASK until approved into the APPROVED
#: list; the Apple Calendar ``create_event`` matches the Google calendar write
#: posture. The family's reads and owner-local creates (reminders, notes, clipboard,
#: …) ALLOW freely.
_DEFAULT_BLACKLIST_TOOLS: tuple[str, ...] = (
    _GMAIL_WRITE_TOOLS
    + _CALENDAR_WRITE_TOOLS
    + _DRIVE_WRITE_TOOLS
    + _SHEETS_WRITE_TOOLS
    + (_WEB_FETCH_TOOL,)
    + _ROUTING_ADMIN_TOOLS
    + _APPLE_CALENDAR_WRITE_TOOLS
    + _APPLE_SHORTCUTS_MUTATING_TOOLS
)

#: Where chief writes its own behavioral overlay (#107, part of #103). Kept as a
#: constant so the field default and the source's fallback can't drift apart. It sits
#: inside ``harness_dir`` on purpose: the harness ``GitVersioner`` stages that whole
#: root, so every self-edit of the overlay is its own revertible commit (#125).
DEFAULT_SELF_CONFIG_PATH = "data/harness/self_config.yaml"

#: Top-level overlay keys chief's own ``self_config.yaml`` may never set (#107). The
#: overlay lets chief rewrite its *behavioral* config at runtime, but the security
#: boundary — the gate blacklists, policy seeds, screening, every opt-in subsystem
#: ``*_enabled`` flag, the owner-identity ids, secrets, the db, MCP endpoints, inbox
#: thread-key routing, and the git-versioning toggles and their versioned roots — stays
#: owner-only. Patterns are matched with :func:`fnmatch.fnmatchcase` against the
#: overlay's top-level keys only; every denied family is a top-level ``Settings``
#: field, so no denied key hides inside a mergeable nested mapping.
#: ``primary_platform`` is denied as an inbox-routing key (it picks the platform the
#: owner inbox lives on); ``self_config_path`` is denied so a self-repointing overlay
#: can't misreport where the resolved config came from.
SELF_CONFIG_DENYLIST: tuple[str, ...] = (
    "blacklist_*",       # blacklist_shell_patterns, blacklist_tools
    "never_seed",
    "approved_seed",
    "screening_*",       # screening_enabled/_model/_block/_tools
    "*_enabled",         # every opt-in subsystem flag (16 fields today)
    "owner_*_id",        # owner_telegram_id/_discord_id/_home_chat_id/_home_guild_id
    "*_token",           # telegram_bot_token, discord_bot_token
    "*_api_key",         # openrouter_api_key, brave_search_api_key
    "db_path",
    # Repointing the control socket relocates a privileged local-control surface —
    # same fence class as db_path (an overlay must not move where chief listens).
    "socket_path",
    # The web UI's listen port (#153): same fence class as socket_path — an overlay
    # must not move where chief listens. (web_enabled / web_lan_enabled are already
    # denied by *_enabled; LAN exposure is owner-only by construction.)
    "web_port",
    # Repointing which database the Messages read tools open is an endpoint move —
    # the same fence class as *_mcp_url (#155). apple_enabled is caught by *_enabled.
    "apple_messages_db_path",
    # The iMessage whitelist's owner tier (#156) is an owner-identity key — the same
    # fence class as owner_*_id. An overlay must never mint itself an owner handle.
    # (imessage_enabled is caught by *_enabled.)
    "imessage_owner_handles",
    "*_mcp_url",         # calendar/drive/sheets/gmail/playwright MCP urls
    "*_thread_key",      # front_desk_thread_key, primary_thread_key
    "primary_platform",  # inbox routing: picks the platform the owner inbox lives on
    # Reversibility is owner-only: not just the *_git toggle, but the path that
    # defines each versioned root — repointing the root (or moving chief's write
    # targets outside it) escapes revert exactly as flipping the toggle off would
    # (#110). memory and harness are treated identically.
    "memory_git",
    "memory_dir",
    "harness_git",
    "harness_dir",
    # The audit log is one of the three controls that survived the sandbox (blacklist +
    # screening + audit log). Repointing it blinds forensics the same way flipping
    # harness_git off escapes revert — same class, same fence (#120).
    "audit_log_path",
    "subagents_dir",     # chief's harness write target — stays inside harness_dir
    "chief_skills_dir",  # chief's harness write target — stays inside harness_dir
    "self_config_path",  # the overlay cannot re-point itself
)


def _overlay_denied(key: str) -> bool:
    """True when a ``self_config.yaml`` top-level key hits the denylist (#107)."""
    return any(fnmatch.fnmatchcase(key, pat) for pat in SELF_CONFIG_DENYLIST)


def parse_hhmm(value: str) -> time | None:
    """Parse a 24-hour ``"HH:MM"`` wall clock, or ``None`` when malformed.

    Shared by the quiet-hours boot validator below and the web settings form
    (#153), so the two can never drift on what counts as a valid bound.
    """
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (ValueError, TypeError):
        return None


#: Review-time allow-list: the explicit complement of ``SELF_CONFIG_DENYLIST`` over
#: ``Settings.model_fields`` (#109). Every ``Settings`` field must be either denied
#: by ``SELF_CONFIG_DENYLIST`` or listed here as merge-safe — the guard test
#: (``tests/test_self_config.py``) fails a field that is in neither set, in both, or
#: listed here but no longer a real field. This is a hand-maintained literal, not
#: computed from the denylist, so it can't silently track the denylist's growth —
#: adding a new ``Settings`` field always requires a deliberate edit here (or to
#: ``SELF_CONFIG_DENYLIST``), which is the point of a review-time default-deny.
MERGE_SAFE: frozenset[str] = frozenset(
    {
        "owner_name",
        "owner_model_default",
        "owner_model_opus",
        "opus_auto_detect",
        "routing_seed",
        "routing_surface_defaults",
        "guest_model",
        "guest_ack",
        "concurrency",
        "turn_timeout_seconds",
        "idle_archive_seconds",
        "compaction_idle_seconds",
        "classifier_model",
        "approval_timeout_seconds",
        "guest_rate_per_window",
        "guest_rate_window_seconds",
        "guest_global_rate_per_window",
        "git_author_name",
        "git_author_email",
        "owner_tz",
        "playwright_screenshots_dir",
        "workspace_dir",
        "shell_timeout_seconds",
        "shell_output_limit",
        "apple_script_timeout_seconds",
        "apple_output_limit",
        "imessage_poll_seconds",
        "imessage_self_dm",
        "web_fetch_timeout_seconds",
        "web_fetch_max_bytes",
        "web_search_count",
        "scheduler_tick_seconds",
        "quiet_hours_start",
        "quiet_hours_end",
        "heartbeat_url",
        "heartbeat_interval_seconds",
        "monitor_min_interval_seconds",
        "monitor_model",
        "premium_request_cap",
        "openrouter_dollar_cap",
        "budget_warn_fractions",
        "budget_exhaust_fraction",
        "budget_downgrade_model",
        "budget_cycle_anchor_day",
        "default_skills",
        "group_context_max_messages",
    }
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
    # configure Telegram, Discord, or both, or neither (the always-on client-plane
    # socket / ``cli`` stack needs no owner id or token).
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
    # Agent backend (#88): the GitHub Copilot SDK is chief's sole harness — the
    # claude-agent-sdk backend and the config ``agent_backend`` selector are gone. A
    # leftover ``agent_backend: copilot`` in an old config.yaml is tolerated (silently
    # dropped, ``extra="ignore"``); ``agent_backend: claude`` fails the boot loudly with
    # a migration message (see ``_reject_removed_agent_backend``).
    db_path: str = "data/chief.db"
    # #130 always-on client-plane listener: chief binds this unix socket whenever it
    # runs (owner-only 0600, unlinked on clean shutdown), independent of any platform.
    socket_path: str = "data/chief.sock"
    guest_ack: str = (
        "Thanks for reaching out — I'm an assistant and I've passed your message along."
    )

    # Web UI (#153): the LAN-served owner cockpit and zero-token day-one channel —
    # ON by default (unlike the opt-in subsystems, this is the surface a fresh
    # install chats through before any platform token exists). Binds 127.0.0.1
    # unless web_lan_enabled explicitly opens it to the LAN (no TLS in v1 — home-LAN
    # threat model; put a reverse proxy in front for anything beyond that). Auth is
    # a single owner password, hashed at rest in the secrets dir next to the
    # platform tokens (never a Settings field).
    web_enabled: bool = True
    web_port: int = 8130
    web_lan_enabled: bool = False

    # Task engine (M2). concurrency bounds turns actively generating;
    # turn_timeout_seconds is the per-turn watchdog (a turn whose stream never
    # terminates is torn down, not left to freeze the task); idle_archive_seconds is the
    # no-activity → archive timer for real task threads; compaction_idle_seconds is the
    # casual-channel twin — instead of archiving (a ``:0`` channel has no closable
    # topic) it self-compacts at this idle window; classifier_model runs the cheap
    # stop-intent / warrants-a-task judgments. It is sent verbatim as OpenRouter's
    # ``model`` field (classify.py), so it MUST be an OpenRouter-namespaced id
    # (``vendor/model``) — a bare ``claude-haiku-4-5`` errors every classifier call and
    # fails safe silently (boot warns; see app.warn_if_classifier_degraded).
    concurrency: int = 3
    turn_timeout_seconds: float = 300.0
    idle_archive_seconds: int = 3600
    compaction_idle_seconds: float = 3600.0
    classifier_model: str = "anthropic/claude-haiku-4.5"

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
    # Same OpenRouter-namespaced-id requirement as classifier_model above (it too is
    # sent verbatim as OpenRouter's ``model``): a bare id fails every screen open
    # (content passes UNSCREENED). Boot warns when it lacks a ``/`` namespace.
    screening_model: str = "anthropic/claude-haiku-4.5"
    screening_block: bool = False
    # MEDIUM-2 fix: Gmail reads (email bodies are attacker-controlled), the Drive read,
    # and the playwright browser action tools that return an updated page
    # snapshot/result alongside the interaction (click/type/hover/drag/drop/
    # select_option/press_key/fill_form/file_upload/handle_dialog) are untrusted
    # channels too, not just the original fetch/search/navigate/snapshot set.
    # ``mcp__chief_web__fetch`` / ``__search`` (#81) are chief's own web tools — their
    # results are internet content, so they must be screened. (#88 dropped the built-in
    # ``WebFetch`` / ``WebSearch`` names: those were claude-agent-sdk's built-ins, which
    # no longer exist under the Copilot backend — chief's own two web tools remain.)
    screening_tools: tuple[str, ...] = (
        _WEB_FETCH_TOOL,
        _WEB_SEARCH_TOOL,
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
        # Apple Messages history reads (#155): message text arrives from other
        # people, the same untrusted channel class as Gmail bodies.
    ) + _APPLE_MESSAGES_READ_TOOLS

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
    # browser_take_screenshot runs. The Apple screenshot tool (#155) writes its
    # captures here too, so every screenshot lands in one place.
    playwright_screenshots_dir: str = "data/screenshots"

    # Apple ecosystem tools (#155), owner-only, darwin-gated. Unlike the other opt-in
    # subsystems, apple_enabled defaults ON: the family is auto-detected — effective
    # only when chief boots on macOS (see the apple_configured property), inert on
    # Linux regardless, and force-off with apple_enabled: false. At boot,
    # per-capability TCC permission probes (chief.tools.apple.doctor) decide which
    # app-area services actually register; the permissions doctor always registers on
    # a Mac. apple_script_timeout_seconds bounds one osascript/shortcuts/sqlite3
    # child process; apple_output_limit caps its captured output;
    # apple_messages_db_path is the Messages store the read-only history tools open
    # (needs Full Disk Access).
    apple_enabled: bool = True
    apple_script_timeout_seconds: float = 30.0
    apple_output_limit: int = 200_000
    apple_messages_db_path: str = "~/Library/Messages/chat.db"

    # iMessage adapter (#156), macOS-only, default OFF (opt-in pattern — unlike
    # apple_enabled it needs real setup first: a dedicated Apple ID signed into
    # Messages on chief's Mac, so it texts as itself and never ghost-writes as the
    # owner). Rides the Apple family's store read layer + ScriptRunner, so it is
    # inert unless apple_configured too; at boot the #155 doctor probes must also
    # pass (Full Disk Access for the store, Automation → Messages for sending).
    # imessage_owner_handles seeds the whitelist owner-tier (E.164 phone numbers
    # and/or emails; the #154 installer wizard writes it) — required when enabled,
    # since guest cards and poller alerts route to the first owner handle.
    # imessage_poll_seconds is the store-poll cadence (the adapter's own tick,
    # well under the scheduler's monitor floor).
    imessage_enabled: bool = False
    imessage_poll_seconds: float = 2.0
    imessage_owner_handles: tuple[str, ...] = ()
    # imessage_self_dm (#161): default OFF. When on (and imessage_configured), the
    # owner's own handle round-trips the full owner path (its self-chat received copy
    # already routes owner-tier), every reply to a self-handle is prefixed "🤖 " and
    # loop-filtered, and chief's own echoed sends are dropped. It is behavior-only —
    # it mints no privilege (owner handles are still gated by imessage_owner_handles)
    # — so it is agent-editable through the self-config overlay, unlike the *_enabled
    # subsystem flags. It requires at least one owner handle, same as imessage_enabled.
    imessage_self_dm: bool = False

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

    # chief-owned web tools (#81, part of #72), owner-only, default off (opt-in).
    # web_tools_enabled wires the in-process ``chief_web`` MCP server (fetch + search)
    # into owner sessions — one server reaching both backends (the Copilot SDK lacks
    # built-in web tools). ``fetch`` is guarded against SSRF (private/loopback/
    # link-local targets refused, the connection pinned to the validated IP, redirects
    # re-validated) and seeded into blacklist_tools so it also asks for approval.
    # ``search`` uses the Brave Search API (brave_search_api_key secret); without the
    # key it degrades to a "not configured" note, so fetch still works.
    # web_fetch_timeout_seconds bounds one fetch and web_fetch_max_bytes caps the bytes
    # read into memory from a single page.
    web_tools_enabled: bool = False
    web_fetch_timeout_seconds: float = 15.0
    web_fetch_max_bytes: int = 5_000_000
    web_search_count: int = 5

    # Scheduler (M9a), default off (mirror the opt-in subsystem pattern). When enabled,
    # the long-running tick fires reminders, recurring jobs, and self-cron the owner set
    # up. primary_thread_key is the owner inbox they land in and primary_platform picks
    # which stack owns the loop — ``telegram``, ``discord``, or ``cli`` (the always-on
    # socket; a tokenless chief can run the scheduler there) — both required by the
    # after-validator. owner_tz (above) frames cron + quiet hours. quiet_hours_*
    # ("HH:MM" in owner_tz) defer a non-urgent fire caught overnight to
    # quiet_hours_end; a None start disables quiet hours. heartbeat_url is an
    # optional dead-man's-switch GET, pinged every
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
    # An agent monitor evaluates its predicate in an ephemeral **Copilot** session
    # (classify.ask_condition), so monitor_model is a Copilot-format id — a DIFFERENT
    # namespace from classifier_model/screening_model (those are OpenRouter-namespaced
    # HTTP one-shots). ``auto`` lets Copilot pick; the Student plan serves ``auto``
    # regardless of any explicit pick (spike #74).
    monitor_model: str = "auto"

    # Usage budgeting (#84, part of #72), default off. chief meters each turn in the
    # native currency it actually spent — no cross-currency conversion. Two currencies
    # carry caps: Copilot premium_request_cap (raw count, 200/mo on the Student plan —
    # spike #74) and openrouter_dollar_cap (metered BYOK spend). Each accumulates vs its
    # cap; crossing a budget_warn_fractions tier warns the owner once, and reaching
    # budget_exhaust_fraction runs that currency's action — premium requests pause the
    # owner's turns + post a choice card, OpenRouter dollars downgrade the openrouter
    # categories onto Copilot budget_downgrade_model (``auto``). A currency configured
    # with a non-positive cap meters but never warns or acts. Warnings + the card route
    # to primary_thread_key, so budget_enabled wants it set (as the scheduler does).
    # budget_cycle_anchor_day
    # (1–28) is the cycle reset day in owner_tz. Inert when disabled.
    budget_enabled: bool = False
    premium_request_cap: int = 200
    openrouter_dollar_cap: float = 20.0
    budget_warn_fractions: tuple[float, ...] = (0.75, 0.90)
    budget_exhaust_fraction: float = 1.0
    budget_downgrade_model: str = "auto"
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

    # Category-routed subagents (#87, part of #72), default off (opt-in pattern). When
    # on, owner sessions under CopilotBackend carry chief's built-in subagents, each
    # declaring a job *category* whose model is resolved through the routing table
    # (routing off ⇒ the subagent runs on the parent model). Owner-only — no guest gets
    # subagents.
    subagents_enabled: bool = False
    # On-disk subagents (#105, part of #103): one ``.md`` per subagent, filename stem =
    # name, YAML frontmatter (``category``, ``description``, optional ``skills``), body
    # = prompt. Resolved through the live routing table at spawn — no restart, no
    # approval card. Seeded from chief's built-in subagents on first boot (empty-dir
    # only) and thereafter the sole source.
    subagents_dir: str = "data/harness/subagents"
    # chief-authored skills root (#106, part of #103): presence of a SKILL.md beneath
    # it makes that dir active for the next owner task; scanned fresh at every owner
    # spawn, gated by skills_enabled. Absent/empty ⇒ only the curated vendored set
    # (default_skills) is handed in.
    chief_skills_dir: str = "data/harness/skills"
    # Common parent of subagents_dir/chief_skills_dir (#110, part of #103): a second,
    # independent GitVersioner roots here so every chief-authored subagent/skill change
    # is a revertible commit — sibling data/chief.db and data/workspace/ sit outside
    # this root and are never staged. harness_git mirrors memory_git's opt-out shape.
    harness_dir: str = "data/harness"
    harness_git: bool = True
    # Chief-authored behavioral overlay (#107, part of #103): a self_config.yaml chief
    # writes for itself, deep-merged over config.yaml at boot by
    # ``SelfConfigSettingsSource`` (below env, so env still wins; secrets are fenced by
    # the denylist's *_token/*_api_key patterns, NOT by source order — see #119).
    # Security keys are denied (``SELF_CONFIG_DENYLIST``). Edits apply lazily at the
    # next restart; an absent or broken file boots clean.
    self_config_path: str = DEFAULT_SELF_CONFIG_PATH

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
    # with their owner id by the configured-platform check. The Copilot CLI manages its
    # own auth in ``~/.copilot/config.json`` (#88 dropped the Claude OAuth token), so no
    # SDK auth secret is a Settings field.
    telegram_bot_token: str | None = None
    discord_bot_token: str | None = None
    # OpenRouter BYOK provider target class (#90, part of #72): the key for the SDK's
    # "openai" provider pointed at OpenRouter. Optional — only required when a session
    # is actually spawned on an ``openrouter`` target. Never the Copilot token itself,
    # which is CLI-managed in ``~/.copilot/config.json`` and never touches secrets_dir.
    openrouter_api_key: str | None = None
    # Brave Search API key for the chief-owned ``web-search`` tool (#81). Optional —
    # only needed when web_tools_enabled and the owner wants search; ``web-fetch`` needs
    # no provider key. Never the Copilot token. Get one at https://brave.com/search/api/.
    brave_search_api_key: str | None = None

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
        "socket_path",
        "audit_log_path",
        "memory_dir",
        "workspace_dir",
        "playwright_screenshots_dir",
        "subagents_dir",
        "chief_skills_dir",
        "harness_dir",
        "self_config_path",
        "apple_messages_db_path",
    )
    @classmethod
    def _expand_user_paths(cls, value: str) -> str:
        """Expand a leading ``~`` so host configs can point at home-dir paths.

        Relative paths stay relative (resolved against the process cwd — the repo
        root under the ``chief`` launcher), matching how ``config.yaml`` is found.
        """
        return os.path.expanduser(value)

    @field_validator("web_port")
    @classmethod
    def _validate_web_port(cls, value: int) -> int:
        """A real TCP port. 0 (ephemeral) is refused — the owner could never find
        the UI after a restart; tests bind ephemerally via ``WebServer`` directly."""
        if not 1 <= value <= 65535:
            raise ValueError(f"web_port must be 1–65535, got {value!r}")
        return value

    @property
    def web_host(self) -> str:
        """The bind address: localhost by default, all interfaces on the LAN toggle."""
        return "0.0.0.0" if self.web_lan_enabled else "127.0.0.1"  # noqa: S104

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

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_agent_backend(cls, data: Any) -> Any:
        """Fail loudly on a leftover ``agent_backend: claude`` (#88 migration).

        The claude-agent-sdk backend is gone; the Copilot SDK is the sole harness. An
        old config selecting ``claude`` would otherwise be silently ignored
        (``extra="ignore"``) and boot onto Copilot with no warning, so it is rejected
        with a migration message instead. ``agent_backend: copilot`` (the value that no
        longer means anything) is tolerated — dropped as an ignored extra. Runs before
        field validation so it fires regardless of which other fields are present. Only
        a yaml/init/env value pydantic-settings collects is seen; a bare
        ``AGENT_BACKEND`` env for the now-absent field is not surfaced here.
        """
        if isinstance(data, dict) and data.get("agent_backend") == "claude":
            raise ValueError(
                "agent_backend: claude is no longer supported — the claude-agent-sdk "
                "backend was removed (#88). chief runs on the GitHub Copilot SDK only. "
                "Remove the agent_backend line from config.yaml (copilot is the only "
                "harness)."
            )
        return data

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
        if parse_hhmm(value) is None:
            raise ValueError(f"quiet hours must be 24-hour HH:MM, got {value!r}")
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

    @field_validator("premium_request_cap")
    @classmethod
    def _validate_premium_cap(cls, value: int) -> int:
        """The premium-request cap must be positive — the gate divides by it."""
        if value <= 0:
            raise ValueError(f"premium_request_cap must be > 0, got {value!r}")
        return value

    @field_validator("openrouter_dollar_cap")
    @classmethod
    def _validate_openrouter_cap(cls, value: float) -> float:
        """The OpenRouter dollar cap must be positive — the gate divides by it."""
        if value <= 0:
            raise ValueError(f"openrouter_dollar_cap must be > 0, got {value!r}")
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

    @field_validator("apple_script_timeout_seconds", "apple_output_limit")
    @classmethod
    def _validate_apple_bounds(cls, value: float) -> float:
        """The Apple runner's timeout and output cap must be positive — a zero or
        negative bound would kill every child process (or drop all output)."""
        if value <= 0:
            raise ValueError(
                f"apple runner bounds must be > 0, got {value!r}"
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

    @property
    def apple_configured(self) -> bool:
        """True iff the Apple tool family should exist: enabled AND on macOS (#155).

        The cross-field check pairs the flag with the platform the process actually
        runs on — a Mac boot grows the family with zero config, a Linux boot is
        silently inert regardless, and ``apple_enabled: false`` is the force-off.
        """
        return self.apple_enabled and sys.platform == "darwin"

    @property
    def imessage_configured(self) -> bool:
        """True iff the iMessage adapter should exist (#156): the flag is on AND
        the Apple family is live (enabled + macOS — the adapter rides its store
        read layer and ScriptRunner). Boot additionally gates on the doctor's
        permission probes (:func:`chief.adapters.imessage.imessage_ready`)."""
        return self.imessage_enabled and self.apple_configured

    @field_validator("imessage_poll_seconds")
    @classmethod
    def _validate_imessage_poll(cls, value: float) -> float:
        """The poll cadence must be positive — 0 would spin the loop hot."""
        if value <= 0:
            raise ValueError(
                f"imessage_poll_seconds must be > 0, got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _require_owner_handles_for_imessage(self) -> "Settings":
        """The iMessage adapter needs at least one owner handle (#156, #161).

        The owner's handles seed the whitelist owner-tier, and the first one is
        the adapter's Front Desk — where guest admission/draft cards and poller
        failure alerts land. Enabled with none configured, every card would have
        nowhere to route (mirrors ``_require_front_desk_for_guests``).
        ``imessage_self_dm`` (#161) equally depends on an owner handle: the
        self-chat *is* an owner handle, so with none set self-DM has no thread.
        """
        if (self.imessage_enabled or self.imessage_self_dm) and not any(
            handle.strip() for handle in self.imessage_owner_handles
        ):
            raise ValueError(
                "imessage_enabled / imessage_self_dm require imessage_owner_handles"
                " — the owner's handles seed the whitelist, route guest cards and "
                "poller alerts, and are the self-DM thread."
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
        nowhere to land; and ``primary_platform`` must be ``cli`` (always available —
        the client-plane socket is always-on infrastructure) or a fully configured chat
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
            "imessage": self.imessage_configured,
            # #139: the client-plane socket is always-on infrastructure (#130) and the
            # CLI stack is built unconditionally (app.build_cli_stack), so ``cli`` is
            # always configured — a tokenless chief can run the scheduler, and its
            # fires land on the socket bus (replayed from the message log when no
            # client is attached).
            CLI_PLATFORM: True,
        }
        if not configured.get(self.primary_platform):
            raise ValueError(
                f"scheduler_enabled needs primary_platform={self.primary_platform!r} "
                "to be a fully configured chat platform (owner id + bot token)."
            )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # ``yaml_source`` is passed twice on purpose: once as a path lookup (to resolve
        # self_config_path from config.yaml) and once as the config.yaml source itself.
        # It caches its file read at construction, so the extra lookup call is free.
        yaml_source = YamlConfigSettingsSource(settings_cls)
        return (
            init_settings,
            env_settings,
            SelfConfigSettingsSource(
                settings_cls, init_settings, env_settings, yaml_source
            ),
            yaml_source,
            file_secret_settings,
        )


class SelfConfigSettingsSource(PydanticBaseSettingsSource):
    """Chief's own behavioral overlay (#107, part of #103).

    Reads ``self_config_path`` (``data/harness/self_config.yaml`` by default), the file
    chief writes to reconfigure *itself* at runtime, and returns it as a settings source
    that sits between ``env`` and ``config.yaml``. pydantic-settings deep-merges the
    source chain, so an overlay key wins over ``config.yaml`` while env vars still win
    over the overlay.

    **Secrets are protected by the denylist, not by source order** (#119).
    ``file_secret_settings`` sits *last* in the chain — lowest precedence — so the
    overlay outranks it. What actually keeps chief from writing itself a credential is
    that every secret-shaped field matches a :data:`SELF_CONFIG_DENYLIST` pattern
    (``*_token``, ``*_api_key``), and the ``test_every_settings_field_classified``
    guard (#109) forces any newly-added field to be denied or explicitly marked
    merge-safe in :data:`MERGE_SAFE`. Do not add a secret field whose name escapes
    those patterns.

    The security-relevant families (:data:`SELF_CONFIG_DENYLIST`) are stripped *before*
    the merge, with one warning naming every dropped key. A missing, empty, or broken
    overlay yields ``{}`` — chief always boots on ``config.yaml`` alone.

    **A type-invalid value is not "broken" in that sense — it fails the boot** (#122).
    The drops above are for a file that is unusable (absent, unparseable, not a mapping)
    or hostile (a denied key); each is dropped so a *usable* config survives. A
    well-formed overlay carrying ``concurrency: banana`` is neither. It is a plain
    config typo, and this module fails the boot on those by long-standing convention
    (see ``_validate_blacklist_patterns``, ``_validate_hhmm``). That is the fail-loud
    choice, not the fragile one: chief failing to boot stops the heartbeat, so the
    external dead-man's switch pages the owner within one interval. Skipping the value
    instead would leave chief running on a stale setting while its own overlay claims
    otherwise, and the only signal would be a log line nobody reads.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *path_lookups: PydanticBaseSettingsSource,
    ) -> None:
        super().__init__(settings_cls)
        # Sources consulted (in precedence order) only to resolve self_config_path:
        # init kwargs > env > config.yaml, falling back to DEFAULT_SELF_CONFIG_PATH.
        self._path_lookups = path_lookups

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        # Required abstract method; unused — ``__call__`` is overridden wholesale.
        return (None, field_name, False)

    def _resolve_path(self) -> Path:
        for source in self._path_lookups:
            value = source().get("self_config_path")
            if value:
                return Path(os.path.expanduser(str(value)))
        return Path(DEFAULT_SELF_CONFIG_PATH)

    def __call__(self) -> dict[str, Any]:
        path = self._resolve_path()
        if not path.is_file():
            return {}  # absence is the normal state — silent.
        try:
            loaded = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            logger.warning("self_config overlay at %s is unparseable: %s", path, exc)
            return {}
        if loaded is None:
            return {}  # empty file — silent.
        if not isinstance(loaded, dict):
            logger.warning(
                "self_config overlay at %s is not a mapping (got %s); ignoring.",
                path,
                type(loaded).__name__,
            )
            return {}
        non_str = [k for k in loaded if not isinstance(k, str)]
        if non_str:
            logger.warning(
                "self_config overlay at %s has non-string keys %s; dropping them.",
                path,
                sorted(repr(k) for k in non_str),
            )
            loaded = {k: v for k, v in loaded.items() if isinstance(k, str)}
        dropped = sorted(k for k in loaded if _overlay_denied(k))
        if dropped:
            logger.warning(
                "self_config overlay at %s tried to set denied keys %s; dropping them.",
                path,
                dropped,
            )
        return {k: v for k, v in loaded.items() if not _overlay_denied(k)}
