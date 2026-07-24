"""The wizard's individual steps: password, model auth, budget cap, updates.

Each one is idempotent and reports ``set`` / ``kept`` / ``skipped``. Split from
:mod:`.wizard`, which owns the IO surface and the order they run in.

Config writes are surgical regex edits rather than a re-dump: ``config.yaml``
is seeded from the commented template, and a round-trip through the YAML
writer would strip every comment explaining the keys.
"""

import re
from collections.abc import Mapping
from pathlib import Path

from chief.install.wizard_io import MIN_PASSWORD_LENGTH, KeyValidator, WizardIO

#: What the wizard offers when the owner accepts scheduled updates: every day
#: at 09:00 UTC. Cron specs are UTC; chief retunes its own schedule later.
DEFAULT_UPDATE_SPEC = "0 9 * * *"


def write_secret(secrets_dir: Path, name: str, value: str) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    path = secrets_dir / name
    path.write_text(value + "\n")
    path.chmod(0o600)


def password_step(
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
        write_secret(secrets_dir, "web_password", from_env)
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
        write_secret(secrets_dir, "web_password", password)
        io.say("owner password: set.")
        return "set"


def key_step(
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
            write_secret(secrets_dir, "openrouter_api_key", env_key)
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
            write_secret(secrets_dir, "openrouter_api_key", key)
            io.say("model auth: OpenRouter key validated and saved.")
            return "set"
        io.say(f"  {error} — check the key and try again.")


def budget_step(
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


def update_step(
    config_path: Path, io: WizardIO, *, interactive: bool, env: Mapping[str, str]
) -> str:
    """Offer chief a schedule for keeping itself on the newest core release.

    Only the schedule is asked about. ``update.autonomy`` ships at
    ``clean-only`` — apply clean updates, ask before resolving a collision —
    which is the safe default and needs no question at install time.
    """
    text = config_path.read_text() if config_path.exists() else ""
    match = re.search(r"^(\s*)schedule:\s*(.*)$", text, re.M)
    if match is None:
        io.say("auto-update: no update.schedule line in config.yaml — skipped.")
        return "skipped"
    if match.group(2).strip() not in ("", '""', "''"):
        io.say(f"auto-update: already scheduled ({match.group(2).strip()}).")
        return "kept"
    spec = env.get("CHIEF_UPDATE_SCHEDULE", "").strip()
    if not spec and interactive:
        io.say(
            "chief can pick up new core releases itself, applying them onto "
            "its own edits and restarting under the done-check."
        )
        answer = io.prompt("  check daily at 09:00 UTC? [Y/n]: ").strip().lower()
        spec = DEFAULT_UPDATE_SPEC if answer in ("", "y", "yes") else ""
    if not spec:
        io.say("auto-update: off (set update.schedule in config.yaml to enable).")
        return "skipped"
    updated = (
        text[: match.start()]
        + f'{match.group(1)}schedule: "{spec}"'
        + text[match.end():]
    )
    config_path.write_text(updated)
    io.say(f"auto-update: scheduled ({spec}, UTC).")
    return "set"
