"""The config load path: coerce config.yaml + env into a typed Config.

One strict yaml read (duplicate keys raise), then every field passed
explicitly into :class:`chief.config.schema.Config` — which is why editing a
``default_factory`` on the schema alone is a silent no-op.
"""

import os
from pathlib import Path
from typing import Any

import yaml

from chief.config import coerce
from chief.config.schema import Config, ConfigError


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
    update = raw.get("update") or {}
    stream = raw.get("stream") or {}
    imessage_mode = coerce.imessage_mode(imessage.get("mode", "self"))
    self_handles = coerce.as_handles(imessage.get("self_handles"))
    owner_db_path = (
        Path(str(imessage["owner_db_path"]))
        if imessage.get("owner_db_path")
        else None
    )
    if imessage_mode == "dedicated" and owner_db_path and not self_handles:
        # Fail closed like imessage.mode itself: without self_handles, chief's
        # own replies come back out of the owner's store as ordinary inbound
        # rows — BOT_PREFIX is already off in dedicated mode — and reach the bus.
        raise ConfigError(
            "imessage.owner_db_path is set but imessage.self_handles is empty "
            "— chief would read its own replies back as input; set both or "
            "neither (docs/CONFIG.md)"
        )
    return Config(
        models=models,
        temperature=temperature,
        db_path=Path(_env_or(raw, "db_path", "data/chief.db")),
        socket_path=Path(_env_or(raw, "socket_path", "data/chief.sock")),
        max_concurrent_sessions=int(_env_or(raw, "max_concurrent_sessions", 4)),
        openrouter_api_key=os.environ.get(
            "OPENROUTER_API_KEY", coerce.read_secret(Path("secrets/openrouter_api_key"))
        ),
        provider_base_url=str(
            _env_or(raw, "provider_base_url", "https://openrouter.ai/api/v1")
        ),
        provider_backends=coerce.backends(raw.get("provider_backends") or {}),
        provider_aliases=coerce.aliases(raw.get("provider_aliases") or {}),
        gate_never=tuple(gate.get("never") or ()),
        gate_approved=tuple(gate.get("approved") or ()),
        gate_announce=bool(gate.get("announce", True)),
        budget_cap_usd=float(budget.get("cap_usd", 0.0)),
        budget_warn_ratio=float(budget.get("warn_ratio", 0.8)),
        quiet_hours=str(raw.get("quiet_hours") or ""),
        web_host=str(_env_or(raw, "web_host", "127.0.0.1")),
        web_port=int(_env_or(raw, "web_port", 8130)),
        web_password=os.environ.get(
            "CHIEF_WEB_PASSWORD", coerce.read_secret(Path("secrets/web_password"))
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
        imessage_owner_handles=coerce.as_handles(imessage.get("owner_handles")),
        imessage_db_path=Path(
            imessage.get("db_path") or Path.home() / "Library/Messages/chat.db"
        ),
        imessage_poll_seconds=float(imessage.get("poll_seconds", 2.0)),
        imessage_owner_db_path=owner_db_path,
        imessage_self_handles=self_handles,
        imessage_mode=imessage_mode,
        compaction_ratio=coerce.ratio(compaction.get("ratio", 0.95)),
        compaction_keep_recent=int(compaction.get("keep_recent", 20)),
        compaction_default_window=int(compaction.get("default_window", 60_000)),
        compaction_windows=coerce.windows(compaction.get("windows") or {}),
        update_autonomy=coerce.autonomy(update.get("autonomy", "clean-only")),
        update_schedule=str(update.get("schedule") or ""),
        stream_channel_defaults=coerce.stream_channel_defaults(
            stream.get("channel_defaults")
        ),
    )


def _env_or(raw: dict[str, Any], key: str, default: Any) -> Any:
    return os.environ.get(f"CHIEF_{key.upper()}", raw.get(key, default))
