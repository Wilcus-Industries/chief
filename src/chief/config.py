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
        default_factory=lambda: {"default": "anthropic/claude-sonnet-4.5"}
    )
    db_path: Path = Path("data/chief.db")
    socket_path: Path = Path("data/chief.sock")
    max_concurrent_sessions: int = 4
    openrouter_api_key: str = ""
    gate_never: tuple[str, ...] = ()
    gate_approved: tuple[str, ...] = ()
    budget_cap_usd: float = 0.0
    budget_warn_ratio: float = 0.8
    quiet_hours: str = ""

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
    return Config(
        models=models,
        db_path=Path(_env_or(raw, "db_path", "data/chief.db")),
        socket_path=Path(_env_or(raw, "socket_path", "data/chief.sock")),
        max_concurrent_sessions=int(_env_or(raw, "max_concurrent_sessions", 4)),
        openrouter_api_key=os.environ.get(
            "OPENROUTER_API_KEY", _read_secret(Path("secrets/openrouter_api_key"))
        ),
        gate_never=tuple(gate.get("never") or ()),
        gate_approved=tuple(gate.get("approved") or ()),
        budget_cap_usd=float(budget.get("cap_usd", 0.0)),
        budget_warn_ratio=float(budget.get("warn_ratio", 0.8)),
        quiet_hours=str(raw.get("quiet_hours") or ""),
    )


def _env_or(raw: dict[str, Any], key: str, default: Any) -> Any:
    return os.environ.get(f"CHIEF_{key.upper()}", raw.get(key, default))


def _read_secret(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""
