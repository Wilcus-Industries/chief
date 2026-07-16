"""The first-run wizard: owner password, OpenRouter key, budget cap.

Idempotent on re-runs — existing state is kept, never destroyed. Secrets are
one file each under ``secrets/`` (the same files ``chief.config`` reads); the
budget cap is written into ``config.yaml`` in place. Every side effect is
injectable so the wizard tests without a TTY or network.
"""

import getpass
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

MIN_PASSWORD_LENGTH = 8
KEY_STATUS_URL = "https://openrouter.ai/api/v1/key"

#: Returns None when the key works, else a short error message.
KeyValidator = Callable[[str], str | None]


@dataclass
class WizardIO:
    """The wizard's whole terminal surface, injectable for tests."""

    prompt: Callable[[str], str] = input
    prompt_secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print


def validate_openrouter_key(key: str) -> str | None:
    """One real key-status call against OpenRouter."""
    try:
        response = httpx.get(
            KEY_STATUS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=10
        )
    except httpx.HTTPError as exc:
        return f"could not reach OpenRouter ({exc})"
    return None if response.status_code == 200 else "OpenRouter rejected the key"


def _write_secret(secrets_dir: Path, name: str, value: str) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    path = secrets_dir / name
    path.write_text(value + "\n")
    path.chmod(0o600)


def _password_step(
    secrets_dir: Path, io: WizardIO, *, interactive: bool, env: Mapping[str, str]
) -> str:
    if (secrets_dir / "web_password").exists():
        io.say("owner password: already set — keeping it.")
        return "kept"
    from_env = env.get("CHIEF_OWNER_PASSWORD", "")
    if from_env:
        if len(from_env) < MIN_PASSWORD_LENGTH:
            io.say("owner password: CHIEF_OWNER_PASSWORD too short — skipped.")
            return "skipped"
        _write_secret(secrets_dir, "web_password", from_env)
        return "set"
    if not interactive:
        io.say(
            "owner password: not set — the web UI stays locked (fail closed) "
            "until you re-run `chief wizard`."
        )
        return "skipped"
    io.say("Choose the owner password — it is the web UI login.")
    while True:
        password = io.prompt_secret(
            f"  password (min {MIN_PASSWORD_LENGTH} chars): "
        )
        if len(password) < MIN_PASSWORD_LENGTH:
            io.say(f"  must be at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        if password != io.prompt_secret("  confirm: "):
            io.say("  passwords do not match — try again.")
            continue
        _write_secret(secrets_dir, "web_password", password)
        io.say("owner password: set.")
        return "set"


def _key_step(
    secrets_dir: Path,
    io: WizardIO,
    *,
    interactive: bool,
    env: Mapping[str, str],
    validate: KeyValidator,
) -> str:
    if (secrets_dir / "openrouter_api_key").exists() or env.get(
        "OPENROUTER_API_KEY"
    ):
        io.say("model auth: OpenRouter key found — keeping it.")
        return "kept"
    env_key = env.get("CHIEF_OPENROUTER_KEY", "").strip()
    if env_key:
        error = validate(env_key)
        if error is None:
            _write_secret(secrets_dir, "openrouter_api_key", env_key)
            io.say("model auth: OpenRouter key (from env) validated and saved.")
            return "set"
        io.say(f"model auth: CHIEF_OPENROUTER_KEY rejected — {error}")
        return "skipped"
    if not interactive:
        io.say("model auth: none — chatting needs an OpenRouter key; re-run "
               "`chief wizard`.")
        return "skipped"
    io.say("chief needs an OpenRouter API key to chat "
           "(https://openrouter.ai/settings/keys).")
    while True:
        key = io.prompt_secret("  OpenRouter API key (empty to skip): ").strip()
        if not key:
            return "skipped"
        error = validate(key)
        if error is None:
            _write_secret(secrets_dir, "openrouter_api_key", key)
            io.say("model auth: OpenRouter key validated and saved.")
            return "set"
        io.say(f"  {error} — check the key and try again.")


def _budget_step(
    config_path: Path, io: WizardIO, *, interactive: bool, env: Mapping[str, str]
) -> str:
    """Set ``budget.cap_usd`` in config.yaml (PRD: the cap is a bootstrap ask)."""
    text = config_path.read_text() if config_path.exists() else ""
    match = re.search(r"^(\s*)cap_usd:\s*([\d.]+)\s*$", text, re.M)
    if match is None:
        io.say("budget cap: no cap_usd line in config.yaml — skipped.")
        return "skipped"
    if float(match.group(2)) > 0:
        io.say(f"budget cap: already ${match.group(2)}/month — keeping it.")
        return "kept"
    raw = env.get("CHIEF_BUDGET_CAP", "").strip()
    if not raw and interactive:
        io.say("Set a monthly OpenRouter spend cap (crossing 80% notifies you).")
        raw = io.prompt("  cap in dollars per month (empty = unlimited): ").strip()
    if not raw:
        io.say("budget cap: unlimited (edit budget.cap_usd in config.yaml).")
        return "skipped"
    try:
        cap = float(raw)
    except ValueError:
        io.say(f"budget cap: {raw!r} is not a number — skipped.")
        return "skipped"
    updated = (
        text[: match.start()] + f"{match.group(1)}cap_usd: {cap:g}"
        + text[match.end():]
    )
    config_path.write_text(updated)
    io.say(f"budget cap: ${cap:g}/month.")
    return "set"


@dataclass(frozen=True)
class WizardResult:
    """What each step did: ``set`` / ``kept`` / ``skipped``."""

    password: str
    model_auth: str
    budget: str


def run_wizard(
    *,
    secrets_dir: Path,
    config_path: Path,
    io: WizardIO,
    interactive: bool,
    env: Mapping[str, str],
    validate: KeyValidator = validate_openrouter_key,
) -> WizardResult:
    """Run the three steps; headless installs drive them via env vars
    (CHIEF_OWNER_PASSWORD, CHIEF_OPENROUTER_KEY, CHIEF_BUDGET_CAP)."""
    return WizardResult(
        password=_password_step(secrets_dir, io, interactive=interactive, env=env),
        model_auth=_key_step(
            secrets_dir, io, interactive=interactive, env=env, validate=validate
        ),
        budget=_budget_step(config_path, io, interactive=interactive, env=env),
    )
