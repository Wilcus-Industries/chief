"""The interactive first-run wizard (#154): owner password + model auth.

Replaces the old manual secrets checklist. Two steps, both idempotent on re-runs
(existing state is kept, never destroyed):

- **Owner password** — becomes the web UI credential. It is written through the web
  surface's own :class:`chief.web.auth.WebAuth`, so the at-rest format can never
  drift from what ``/login`` verifies. Headless installs pass it via the
  ``CHIEF_OWNER_PASSWORD`` env var, or skip it — the browser's first-visit
  ``/setup`` page captures it then.
- **Model auth** — one of chief's two model classes: the GitHub Copilot CLI login
  (walked mid-flow with re-checks; the CLI writes ``~/.copilot/config.json``) or an
  OpenRouter API key (validated with one real key-status call before being written
  to the secrets dir). No platform bot tokens are requested — the web UI is the
  day-one channel; Telegram/Discord connect later from the web settings pages.

Every side effect is injected (prompts, the validator, the login probe) so the
wizard is unit-testable without a TTY or network.
"""

import asyncio
import getpass
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..web.auth import MIN_PASSWORD_LENGTH, WebAuth
from ..web.settings_io import SecretsStore, validate_openrouter_key

#: The Copilot CLI's own login state — CLI-managed, never a chief secret
#: (see ``secrets/README.md``: "Not here: the agent token").
COPILOT_CONFIG = Path("~/.copilot/config.json")

#: The one-file-per-secret name the settings loader reads for OpenRouter.
OPENROUTER_SECRET = "openrouter_api_key"

#: A model-auth validator: ``None`` when the credential works, else an error.
KeyValidator = Callable[[str], Awaitable[str | None]]


def copilot_logged_in(config_path: Path = COPILOT_CONFIG) -> bool:
    """True when the Copilot CLI has a login on this machine."""
    return config_path.expanduser().is_file()


@dataclass
class WizardIO:
    """The wizard's whole terminal surface, injectable for tests."""

    prompt: Callable[[str], str] = input
    prompt_secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print


@dataclass(frozen=True)
class WizardResult:
    """What each step did: ``set``/``kept``/``skipped`` (+ the model path chosen)."""

    password: str
    model_auth: str


def _password_step(
    auth: WebAuth, io: WizardIO, *, interactive: bool, env: Mapping[str, str]
) -> str:
    if auth.password_set:
        io.say("owner password: already set — keeping it (change it in the web "
               "UI under Settings).")
        return "kept"
    env_password = env.get("CHIEF_OWNER_PASSWORD", "")
    if env_password:
        if len(env_password) < MIN_PASSWORD_LENGTH:
            io.say(
                "owner password: CHIEF_OWNER_PASSWORD must be at least "
                f"{MIN_PASSWORD_LENGTH} characters — ignored; set it at the "
                "first browser visit (/setup)."
            )
            return "skipped"
        auth.set_password(env_password)
        return "set"
    if not interactive:
        io.say(
            "owner password: not set — the web UI's first-visit /setup page "
            "will capture it."
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
        confirm = io.prompt_secret("  confirm: ")
        if password != confirm:
            io.say("  passwords do not match — try again.")
            continue
        auth.set_password(password)
        io.say("owner password: set.")
        return "set"


def _validate_key_sync(validate: KeyValidator, key: str) -> str | None:
    async def call() -> str | None:
        return await validate(key)

    return asyncio.run(call())


def _openrouter_paste(
    secrets: SecretsStore, io: WizardIO, validate: KeyValidator
) -> str:
    io.say("Get a key at https://openrouter.ai/settings/keys")
    while True:
        key = io.prompt_secret("  OpenRouter API key (empty to skip): ").strip()
        if not key:
            return "skipped"
        error = _validate_key_sync(validate, key)
        if error is None:
            secrets.write(OPENROUTER_SECRET, key)
            io.say("model auth: OpenRouter key validated and saved.")
            return "openrouter"
        io.say(f"  {error} — check the key and try again.")


def _copilot_walk(io: WizardIO, copilot_check: Callable[[], bool]) -> str:
    while True:
        if copilot_check():
            io.say("model auth: GitHub Copilot login found.")
            return "copilot"
        io.say(
            "No Copilot login yet. In another terminal, run the GitHub "
            "Copilot CLI (`copilot`) and complete /login — install it first "
            "if needed: https://docs.github.com/copilot/concepts/agents/"
            "about-copilot-cli"
        )
        reply = io.prompt("  press Enter to re-check, or 's' to skip: ").strip()
        if reply.lower() == "s":
            return "skipped"


def _model_auth_step(
    secrets_dir: Path,
    io: WizardIO,
    *,
    interactive: bool,
    copilot_check: Callable[[], bool],
    validate_openrouter: KeyValidator,
    env: Mapping[str, str],
) -> str:
    secrets = SecretsStore(secrets_dir)
    if copilot_check():
        io.say("model auth: GitHub Copilot login found — keeping it.")
        return "kept"
    if secrets.exists(OPENROUTER_SECRET) or env.get("OPENROUTER_API_KEY"):
        io.say("model auth: OpenRouter key found — keeping it.")
        return "kept"
    env_key = env.get("CHIEF_OPENROUTER_KEY", "").strip()
    if env_key:
        error = _validate_key_sync(validate_openrouter, env_key)
        if error is None:
            secrets.write(OPENROUTER_SECRET, env_key)
            io.say("model auth: OpenRouter key (from env) validated and saved.")
            return "openrouter"
        io.say(f"model auth: CHIEF_OPENROUTER_KEY rejected — {error}")
        return "skipped"
    if not interactive:
        io.say(
            "model auth: none configured — chatting needs a GitHub Copilot "
            "login or an OpenRouter key. Re-run `chief wizard`, or add a key "
            "in the web UI settings."
        )
        return "skipped"
    io.say(
        "chief needs model auth to chat — either a GitHub Copilot "
        "subscription (free tier works, with limits) or an OpenRouter key."
    )
    while True:
        choice = io.prompt(
            "  [1] GitHub Copilot login  [2] OpenRouter API key: "
        ).strip()
        if choice == "1":
            return _copilot_walk(io, copilot_check)
        if choice == "2":
            return _openrouter_paste(secrets, io, validate_openrouter)
        io.say("  answer 1 or 2.")


def run_wizard(
    *,
    secrets_dir: Path,
    io: WizardIO,
    interactive: bool,
    copilot_check: Callable[[], bool] = copilot_logged_in,
    validate_openrouter: KeyValidator = validate_openrouter_key,
    env: Mapping[str, str] | None = None,
) -> WizardResult:
    """Run both wizard steps against ``secrets_dir``; returns what each did.

    ``interactive=False`` is the headless (``curl | bash`` in CI / agent-run)
    path: env vars drive it (``CHIEF_OWNER_PASSWORD``, ``CHIEF_OPENROUTER_KEY``)
    and anything unanswerable is skipped with a pointer, never a hang.
    """
    if env is None:
        env = os.environ
    password = _password_step(
        WebAuth(secrets_dir), io, interactive=interactive, env=env
    )
    model_auth = _model_auth_step(
        secrets_dir,
        io,
        interactive=interactive,
        copilot_check=copilot_check,
        validate_openrouter=validate_openrouter,
        env=env,
    )
    return WizardResult(password=password, model_auth=model_auth)
