"""The config load path: coerce config.yaml + env into a typed Config.

One strict yaml read (duplicate keys raise), then every field passed
explicitly into :class:`chief.config.schema.Config` — which is why editing a
``default_factory`` on the schema alone is a silent no-op.
"""

import os
from pathlib import Path
from typing import Any

import yaml

from chief.config.schema import AliasSpec, BackendSpec, Config, ConfigError


class _StrictLoader(yaml.SafeLoader):  # type: ignore[misc]  # yaml is untyped
    """SafeLoader that raises on duplicate mapping keys.

    PyYAML silently keeps the last duplicate, so a shell-appended second copy
    of a config block "parses fine" and every check passes — three appended
    ``obsidian_memory:`` blocks did exactly that in prod. Rejecting the
    duplicate makes the *second* append fail loudly at the restart gate.
    """


def _no_duplicates(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise ConfigError(
                f"duplicate key {key!r} in config.yaml (line "
                f"{key_node.start_mark.line + 1}) — the same block was written "
                "twice; merge the copies (use chief.config_apply, never append)"
            )
        seen.add(key)
    return dict(loader.construct_mapping(node, deep=True))


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates
)


def load_raw(path: Path = Path("config.yaml")) -> dict[str, Any]:
    """Parse config.yaml into a raw mapping (``{}`` when the file is absent).

    The single yaml read the loader and the hooks phase both go through, so a
    package's ``config_keys`` are sliced from the exact same source of truth.
    Duplicate top-level or nested keys raise :class:`ConfigError` (see
    :class:`_StrictLoader`).
    """
    if path.exists():
        return yaml.load(path.read_text(), Loader=_StrictLoader) or {}
    return {}


def load_config(path: Path = Path("config.yaml")) -> Config:
    """Load config.yaml (if present), then apply env overrides."""
    raw = load_raw(path)
    raw_models = dict(raw.get("models") or {})
    temperature = float(raw_models.pop("temperature", 0.0))
    models = dict(Config().models) | raw_models
    gate = raw.get("gate") or {}
    budget = raw.get("budget") or {}
    imessage = raw.get("imessage") or {}
    shell = raw.get("shell") or {}
    hooks = raw.get("hooks") or {}
    compaction = raw.get("compaction") or {}
    return Config(
        models=models,
        temperature=temperature,
        db_path=Path(_env_or(raw, "db_path", "data/chief.db")),
        socket_path=Path(_env_or(raw, "socket_path", "data/chief.sock")),
        max_concurrent_sessions=int(_env_or(raw, "max_concurrent_sessions", 4)),
        openrouter_api_key=os.environ.get(
            "OPENROUTER_API_KEY", _read_secret(Path("secrets/openrouter_api_key"))
        ),
        provider_base_url=str(
            _env_or(raw, "provider_base_url", "https://openrouter.ai/api/v1")
        ),
        provider_backends=_parse_backends(raw.get("provider_backends") or {}),
        provider_aliases=_parse_aliases(raw.get("provider_aliases") or {}),
        gate_never=tuple(gate.get("never") or ()),
        gate_approved=tuple(gate.get("approved") or ()),
        gate_announce=bool(gate.get("announce", True)),
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
        classifiers_dir=Path(_env_or(raw, "classifiers_dir", "classifiers")),
        mcp_servers=dict(raw.get("mcp_servers") or {}),
        packages_dir=Path(_env_or(raw, "packages_dir", "packages")),
        packages_repo=str(_env_or(raw, "packages_repo", Config().packages_repo)),
        shell_timeout_seconds=float(shell.get("timeout_seconds", 20.0)),
        shell_output_limit=int(shell.get("output_limit", 30_000)),
        hooks_timeout_seconds=float(hooks.get("timeout_seconds", 10.0)),
        hooks_disabled=tuple(hooks.get("disabled") or ()),
        imessage_enabled=bool(imessage.get("enabled", False)),
        imessage_owner_handles=_as_handles(imessage.get("owner_handles")),
        imessage_db_path=Path(
            imessage.get("db_path") or Path.home() / "Library/Messages/chat.db"
        ),
        imessage_poll_seconds=float(imessage.get("poll_seconds", 2.0)),
        compaction_ratio=_parse_ratio(compaction.get("ratio", 0.95)),
        compaction_keep_recent=int(compaction.get("keep_recent", 20)),
        compaction_default_window=int(compaction.get("default_window", 60_000)),
        compaction_windows=_parse_windows(compaction.get("windows") or {}),
    )


def _as_handles(value: Any) -> tuple[str, ...]:
    """Coerce ``imessage.owner_handles`` into a tuple of strings.

    A single quoted string becomes a one-element tuple; a list/tuple becomes
    per-element strings; missing/empty becomes ``()``. A **bare numeric scalar**
    (``owner_handles: +15551234567`` → YAML parses it as the int
    ``15551234567``, silently dropping the ``+``) is rejected with a clear
    ``ConfigError``: coercing it would scope to the wrong chat and fail
    silently, and raising turns the mini boot-loop (``tuple(int)`` TypeError
    deep in boot) into an actionable error. A mapping is likewise rejected.
    """
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(v) for v in value)
    raise ConfigError(
        "imessage.owner_handles must be a quoted handle or a list of quoted "
        f"handles (e.g. [\"+15551234567\"]), got {type(value).__name__} "
        f"{value!r} — quote it so YAML keeps the leading '+'."
    )


def _env_or(raw: dict[str, Any], key: str, default: Any) -> Any:
    return os.environ.get(f"CHIEF_{key.upper()}", raw.get(key, default))


def _read_secret(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""


def _parse_backends(raw: dict[str, Any]) -> dict[str, BackendSpec]:
    return {
        name: BackendSpec(base_url=str(spec["base_url"]), api_key=_backend_key(spec))
        for name, spec in raw.items()
    }


def _backend_key(spec: dict[str, Any]) -> str:
    """Resolve a backend key: ``api_key_env`` var first, then a secret file."""
    env = spec.get("api_key_env")
    if env and os.environ.get(env):
        return os.environ[env]
    if secret := spec.get("api_key_secret"):
        return _read_secret(Path("secrets") / str(secret))
    return ""


def _parse_aliases(raw: dict[str, Any]) -> dict[str, AliasSpec]:
    return {
        name: AliasSpec(backend=str(spec["backend"]), model=str(spec["model"]))
        for name, spec in raw.items()
    }


def _parse_windows(raw: dict[str, Any]) -> dict[str, int]:
    """Coerce the ``compaction.windows`` override map to ``model-name -> tokens``."""
    return {str(name): int(tokens) for name, tokens in raw.items()}


def _parse_ratio(raw: Any) -> float:
    """Coerce ``compaction.ratio`` and clamp it to ``(0, 1]``.

    ``0`` (or negative) would fire compaction every turn; ``>1`` would silently
    disable threshold compaction — both foot-guns, so reject at boot instead.
    """
    ratio = float(raw)
    if not 0 < ratio <= 1:
        raise ConfigError(f"compaction.ratio must be in (0, 1], got {ratio}")
    return ratio
