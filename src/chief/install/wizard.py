"""The first-run wizard: owner password, OpenRouter key, budget cap, updates.

Idempotent on re-runs — existing state is kept, never destroyed. Secrets are
one file each under ``secrets/`` (the same files ``chief.config`` reads); the
budget cap and the update schedule are written into ``config.yaml`` in place.
Every side effect is injectable so the wizard tests without a TTY or network.
The steps themselves live in :mod:`.wizard_steps`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from chief.install import wizard_steps
from chief.install.wizard_io import (
    KEY_STATUS_URL,
    MIN_PASSWORD_LENGTH,
    KeyValidator,
    WizardIO,
)

__all__ = ["MIN_PASSWORD_LENGTH", "WizardIO", "WizardResult", "run_wizard"]


def validate_openrouter_key(key: str) -> str | None:
    """One real key-status call against OpenRouter."""
    try:
        response = httpx.get(
            KEY_STATUS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=10
        )
    except httpx.HTTPError as exc:
        return f"could not reach OpenRouter ({exc})"
    return None if response.status_code == 200 else "OpenRouter rejected the key"


@dataclass(frozen=True)
class WizardResult:
    """What each step did: ``set`` / ``kept`` / ``skipped``."""

    password: str
    model_auth: str
    budget: str
    auto_update: str


def run_wizard(
    *,
    secrets_dir: Path,
    config_path: Path,
    io: WizardIO,
    interactive: bool,
    env: Mapping[str, str],
    validate: KeyValidator = validate_openrouter_key,
) -> WizardResult:
    """Run the four steps; headless installs drive them via env vars
    (CHIEF_OWNER_PASSWORD, CHIEF_OPENROUTER_KEY, CHIEF_BUDGET_CAP,
    CHIEF_UPDATE_SCHEDULE)."""
    return WizardResult(
        password=wizard_steps.password_step(
            secrets_dir, io, interactive=interactive, env=env
        ),
        model_auth=wizard_steps.key_step(
            secrets_dir, io, interactive=interactive, env=env, validate=validate
        ),
        budget=wizard_steps.budget_step(
            config_path, io, interactive=interactive, env=env
        ),
        auto_update=wizard_steps.update_step(
            config_path, io, interactive=interactive, env=env
        ),
    )
