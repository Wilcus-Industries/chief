"""Core configuration: a small flat set of keys from config.yaml, env-overridable.

Models ship with exactly one role — ``default``. The agent invents further
roles via self-config; core never hardcodes any.
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

    @property
    def default_model(self) -> str:
        return self.models["default"]


def load_config(path: Path = Path("config.yaml")) -> Config:
    """Load config.yaml (if present), then apply env overrides."""
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    models = dict(Config().models) | dict(raw.get("models") or {})
    db_path = os.environ.get("CHIEF_DB_PATH", raw.get("db_path", "data/chief.db"))
    socket_path = os.environ.get(
        "CHIEF_SOCKET_PATH", raw.get("socket_path", "data/chief.sock")
    )
    return Config(
        models=models,
        db_path=Path(db_path),
        socket_path=Path(socket_path),
        max_concurrent_sessions=int(
            os.environ.get(
                "CHIEF_MAX_CONCURRENT_SESSIONS", raw.get("max_concurrent_sessions", 4)
            )
        ),
        openrouter_api_key=os.environ.get(
            "OPENROUTER_API_KEY", _read_secret(Path("secrets/openrouter_api_key"))
        ),
    )


def _read_secret(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""
