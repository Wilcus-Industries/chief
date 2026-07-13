"""Curated settings persistence for the web UI (#153) — not a config editor.

Two write targets, matching where each kind of value already lives:

- **Secrets** (platform bot tokens, the OpenRouter key) are one-file-per-secret in
  the secrets dir — the pydantic-settings convention ``chief.app.load_settings``
  reads — written owner-only (0600), never into any yaml.
- **Non-secret flags** (owner ids, the LAN toggle, quiet hours, the default model)
  go to the owner config file (``config.yaml``). Updates are **line-surgical**: an
  existing top-level key's line is replaced in place and a new key is appended, so
  the owner's comments survive a settings edit. Scalars only — the curated forms
  never write nested structure.

This is owner-authored configuration, distinct from chief's own ``self_config.yaml``
overlay; nothing here touches that channel. Tokens are validated before being
written — each validator makes one real call to the platform's identity endpoint —
and every change applies at the next daemon restart (the boot wiring reads settings
once).
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx
import yaml

from ..config import Settings

logger = logging.getLogger(__name__)

#: Owner read/write only — the secrets-dir fence (see ``secrets/README.md``).
_SECRET_MODE: Final[int] = 0o600

_VALIDATE_TIMEOUT: Final[float] = 10.0

#: A token validator: returns ``None`` when the credential works, else a
#: human-readable error. Injected so tests never call the real platform APIs.
Validator = Callable[[str], Awaitable[str | None]]


def write_owner_only(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` with owner-only (0600) permissions.

    touch-then-chmod-then-write so the bytes never exist under a wider mode.
    Shared with :mod:`chief.web.auth`'s credential store.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=_SECRET_MODE, exist_ok=True)
    path.chmod(_SECRET_MODE)
    path.write_text(content)


class SecretsStore:
    """One-file-per-secret writes into the secrets dir."""

    def __init__(self, secrets_dir: Path | str) -> None:
        self._dir = Path(secrets_dir)

    def write(self, name: str, value: str) -> None:
        write_owner_only(self._dir / name, value.strip())

    def delete(self, name: str) -> None:
        (self._dir / name).unlink(missing_ok=True)

    def exists(self, name: str) -> bool:
        return (self._dir / name).is_file()


class OwnerConfig:
    """Line-surgical scalar updates to the owner's ``config.yaml``."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def read(self) -> dict[str, object]:
        """The file's current mapping (empty when absent/unparseable)."""
        if not self._path.is_file():
            return {}
        try:
            loaded = yaml.safe_load(self._path.read_text())
        except yaml.YAMLError as exc:
            logger.warning("owner config at %s unparseable: %s", self._path, exc)
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def update(self, values: Mapping[str, object]) -> None:
        """Set top-level scalar keys, preserving every other line verbatim.

        An existing ``key:`` line is rewritten in place (its position — and every
        comment around it — survives); a new key is appended at the end. Rendering
        goes through ``yaml.safe_dump`` so quoting is always correct (``07:00``
        stays a string, booleans become ``true``/``false``).
        """
        for value in values.values():
            assert value is None or isinstance(value, (str | int | float | bool)), (
                f"curated settings write scalars only, got {type(value).__name__}"
            )
        lines = (
            self._path.read_text().splitlines() if self._path.is_file() else []
        )
        remaining = dict(values)
        for index, line in enumerate(lines):
            stripped = line.split("#", 1)[0]
            if ":" not in stripped:
                continue
            key = stripped.split(":", 1)[0].strip()
            if key in remaining and not line.startswith((" ", "\t")):
                lines[index] = self._render(key, remaining.pop(key))
        for key, value in remaining.items():
            lines.append(self._render(key, value))
        self._path.write_text("\n".join(lines) + "\n")

    @staticmethod
    def _render(key: str, value: object) -> str:
        rendered: str = yaml.safe_dump({key: value}, default_flow_style=False)
        return rendered.strip()


async def _probe(
    url: str, *, headers: dict[str, str] | None = None, what: str
) -> str | None:
    """GET an identity endpoint; ``None`` on 2xx, an error message otherwise."""
    try:
        async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return f"could not reach {what}: {exc}"
    if response.is_success:
        return None
    return f"{what} rejected the credential (HTTP {response.status_code})"


async def validate_telegram_token(token: str) -> str | None:
    """One real ``getMe`` call — the token works iff Telegram says so."""
    return await _probe(
        f"https://api.telegram.org/bot{token}/getMe", what="Telegram"
    )


async def validate_discord_token(token: str) -> str | None:
    """One real ``users/@me`` call with the Bot scheme."""
    return await _probe(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}"},
        what="Discord",
    )


async def validate_openrouter_key(key: str) -> str | None:
    """One real key-status call against OpenRouter."""
    return await _probe(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {key}"},
        what="OpenRouter",
    )


#: Per-platform secrets-file name + owner-id config key (the connect targets).
PLATFORM_FIELDS: Final[dict[str, tuple[str, str]]] = {
    "telegram": ("telegram_bot_token", "owner_telegram_id"),
    "discord": ("discord_bot_token", "owner_discord_id"),
}


@dataclass
class SettingsPanel:
    """Everything the settings routes write with, plus the display snapshot."""

    secrets: SecretsStore
    config: OwnerConfig
    #: The boot-time settings, for displaying current values. Writes do NOT
    #: mutate it — the banner says "restart to apply", and means it.
    settings: Settings
    validators: dict[str, Validator] = field(
        default_factory=lambda: {
            "telegram": validate_telegram_token,
            "discord": validate_discord_token,
            "openrouter": validate_openrouter_key,
        }
    )
