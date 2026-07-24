"""The config schema: every key the daemon knows, as one typed dataclass.

Models ship with exactly one role — ``default``. The agent invents further
roles via self-config; core never hardcodes any (``downgrade`` and
``default_classifier`` are honored when present, never required).

**The dataclass-default trap:** :func:`chief.config.load.load_config` passes
every field explicitly, so editing a ``default_factory`` here without touching
the load path is a silent no-op (see docs/CONFIG.md).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """A config.yaml value is the wrong shape and can't be coerced.

    Raised instead of letting a bad value crash deep in the boot path with an
    opaque ``TypeError`` (the mini boot-loop: a bare ``owner_handles`` scalar
    hit ``tuple(int)``).
    """


@dataclass(frozen=True)
class BackendSpec:
    """A named provider backend: an OpenAI-compatible endpoint plus its key.

    ``api_key`` is resolved AT LOAD from ``api_key_env`` (an env var name) then
    ``api_key_secret`` (a filename under ``secrets/``) — mirroring how
    ``openrouter_api_key`` resolves. No inline keys ever live in the yaml.
    """

    base_url: str
    api_key: str


@dataclass(frozen=True)
class AliasSpec:
    """A typed model name mapped onto ``(backend, real model id)`` for routing."""

    backend: str
    model: str


@dataclass(frozen=True)
class Config:
    """Runtime configuration for the daemon."""

    models: dict[str, str] = field(
        default_factory=lambda: {"default": "qwen/qwen3-coder"}
    )
    temperature: float = 0.0
    db_path: Path = Path("data/chief.db")
    socket_path: Path = Path("data/chief.sock")
    max_concurrent_sessions: int = 4
    openrouter_api_key: str = ""
    # The OpenAI-compatible endpoint the provider streams from. Defaults to
    # OpenRouter; override to a local proxy (e.g. claude-code-openai-server,
    # which serves a Claude subscription in bare mode) to drive Anthropic via
    # OAuth instead of paying per token. Caveat: such a proxy reports no dollar
    # cost, so budget_cap_usd is inert against it. Set openrouter_api_key to the
    # proxy's bearer (e.g. CCI_API_KEY) — it is sent verbatim as Authorization.
    provider_base_url: str = "https://openrouter.ai/api/v1"
    # Named extra backends and typed-name -> (backend, real model) aliases for
    # per-model routing (see RouterProvider). Empty = the single legacy default
    # backend above; the anthropic-oauth package populates them.
    provider_backends: dict[str, "BackendSpec"] = field(default_factory=dict)
    provider_aliases: dict[str, "AliasSpec"] = field(default_factory=dict)
    gate_never: tuple[str, ...] = ()
    gate_approved: tuple[str, ...] = ()
    # Announce every non-card tool call on the session's own surface, so an
    # approved / "always allow"ed tool stays visible instead of silent.
    gate_announce: bool = True
    budget_cap_usd: float = 0.0
    budget_warn_ratio: float = 0.8
    quiet_hours: str = ""
    web_host: str = "127.0.0.1"
    web_port: int = 8130
    web_password: str = ""
    skills_dir: Path = Path("skills")
    agents_dir: Path = Path("agents")
    classifiers_dir: Path = Path("classifiers")
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    packages_dir: Path = Path("packages")
    packages_repo: str = "https://github.com/Wilcus-Industries/chief-packages"
    shell_timeout_seconds: float = 20.0
    shell_output_limit: int = 30_000
    hooks_timeout_seconds: float = 10.0
    hooks_disabled: tuple[str, ...] = ()
    imessage_enabled: bool = False
    imessage_owner_handles: tuple[str, ...] = ()
    imessage_db_path: Path = field(
        default_factory=lambda: Path.home() / "Library/Messages/chat.db"
    )
    imessage_poll_seconds: float = 2.0
    # Context compaction: fold old history into a summary note when a thread's
    # transcript nears the model's context window. Threshold = ratio * window;
    # the window is the thread's *current* model's, resolved per turn (config
    # override -> OpenRouter metadata -> default_window fallback).
    compaction_ratio: float = 0.95
    compaction_keep_recent: int = 20
    compaction_default_window: int = 60_000
    compaction_windows: dict[str, int] = field(default_factory=dict)
    # What a *scheduled* self-update may do with nobody present: off = manual
    # only, clean-only = apply clean updates and ask on a collision, full =
    # resolve collisions unattended and report afterwards. The owner asking
    # chief directly is always allowed, whatever this says.
    update_autonomy: str = "clean-only"
    # Cron spec for chief's own update schedule ("" = none). Config is the
    # source of truth — chief creates the schedule from this and re-creates it
    # when it is missing — so switching scheduled updates off means clearing
    # this key, not only deleting the schedule.
    update_schedule: str = ""

    @property
    def default_model(self) -> str:
        return self.models["default"]
