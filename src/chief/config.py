"""Core configuration: a small flat set of keys from config.yaml, env-overridable.

Models ship with exactly one role — ``default``. The agent invents further
roles via self-config; core never hardcodes any (``downgrade`` and
``cheap-judgment`` are honored when present, never required).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Config:
    """Runtime configuration for the daemon."""

    models: dict[str, str] = field(
        default_factory=lambda: {"default": "qwen/qwen3-coder"}
    )
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
    gate_never: tuple[str, ...] = ()
    gate_approved: tuple[str, ...] = ()
    budget_cap_usd: float = 0.0
    budget_warn_ratio: float = 0.8
    quiet_hours: str = ""
    web_host: str = "127.0.0.1"
    web_port: int = 8130
    web_password: str = ""
    skills_dir: Path = Path("skills")
    agents_dir: Path = Path("agents")
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    packages_dir: Path = Path("packages")
    packages_repo: str = "https://github.com/CrazyWillBear/chief-packages"
    imessage_enabled: bool = False
    imessage_owner_handles: tuple[str, ...] = ()
    imessage_db_path: Path = field(
        default_factory=lambda: Path.home() / "Library/Messages/chat.db"
    )
    imessage_poll_seconds: float = 2.0

    @property
    def default_model(self) -> str:
        return self.models["default"]


def load_config(path: Path = Path("config.yaml")) -> Config:
    """Load config.yaml (if present), then apply env overrides."""
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    models = dict(Config().models) | dict(raw.get("models") or {})
    gate = raw.get("gate") or {}
    budget = raw.get("budget") or {}
    imessage = raw.get("imessage") or {}
    return Config(
        models=models,
        db_path=Path(_env_or(raw, "db_path", "data/chief.db")),
        socket_path=Path(_env_or(raw, "socket_path", "data/chief.sock")),
        max_concurrent_sessions=int(_env_or(raw, "max_concurrent_sessions", 4)),
        openrouter_api_key=os.environ.get(
            "OPENROUTER_API_KEY", _read_secret(Path("secrets/openrouter_api_key"))
        ),
        provider_base_url=str(
            _env_or(raw, "provider_base_url", "https://openrouter.ai/api/v1")
        ),
        gate_never=tuple(gate.get("never") or ()),
        gate_approved=tuple(gate.get("approved") or ()),
        budget_cap_usd=float(budget.get("cap_usd", 0.0)),
        budget_warn_ratio=float(budget.get("warn_ratio", 0.8)),
        quiet_hours=str(raw.get("quiet_hours") or ""),
        web_host=str(_env_or(raw, "web_host", "127.0.0.1")),
        web_port=int(_env_or(raw, "web_port", 8130)),
        web_password=os.environ.get(
            "CHIEF_WEB_PASSWORD", _read_secret(Path("secrets/web_password"))
        ),
        skills_dir=Path(_env_or(raw, "skills_dir", "skills")),
        agents_dir=Path(_env_or(raw, "agents_dir", "agents")),
        mcp_servers=dict(raw.get("mcp_servers") or {}),
        packages_dir=Path(_env_or(raw, "packages_dir", "packages")),
        packages_repo=str(_env_or(raw, "packages_repo", Config().packages_repo)),
        imessage_enabled=bool(imessage.get("enabled", False)),
        imessage_owner_handles=tuple(imessage.get("owner_handles") or ()),
        imessage_db_path=Path(
            imessage.get("db_path") or Path.home() / "Library/Messages/chat.db"
        ),
        imessage_poll_seconds=float(imessage.get("poll_seconds", 2.0)),
    )


def merge_config(updates: dict[str, Any], path: Path = Path("config.yaml")) -> None:
    """Deep-merge ``updates`` into config.yaml, creating it if absent.

    The deterministic config-writer package install scripts call (issue #185)
    so standard keys are set byte-exactly instead of retyped by the model.
    Nested mappings merge key-by-key; every other value is replaced.
    """
    raw = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    path.write_text(yaml.safe_dump(_deep_merge(raw, updates), sort_keys=False))


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _env_or(raw: dict[str, Any], key: str, default: Any) -> Any:
    return os.environ.get(f"CHIEF_{key.upper()}", raw.get(key, default))


def _read_secret(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""
