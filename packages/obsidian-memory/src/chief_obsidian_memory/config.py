"""Typed settings for the obsidian-memory package.

Read from the ``obsidian_memory`` config key the manifest declares: the loader
slices that one top-level key and hands its nested mapping to the hook, which
builds a :class:`MemorySettings` — every sub-key optional, code fills defaults.
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

_DEFAULT_EXCLUDE = (".obsidian/", "templates/", "attachments/")
_TUPLE_KEYS = ("include", "exclude", "vault_paths", "writable_paths")
_PACKAGE_NAME = "obsidian-memory"
_DATA_ROOT = Path("data/hooks")


def package_data_dir() -> Path:
    """The package's on-disk data dir, ``data/hooks/obsidian-memory``.

    Mirrors the hook loader's ``data_root / name`` (``chief.app`` wires
    ``data_root=data/hooks``). The CLI has no ``HookContext``, so it rebuilds
    the same directory from this one place — so it targets the ambient hook's
    index rather than a private one of its own.
    """
    return _DATA_ROOT / _PACKAGE_NAME


def index_home_for(data_dir: Path) -> Path:
    """The chroma index home under a package data dir. The single derivation
    the ambient hook and the CLI share, so both open the same collection."""
    return data_dir / "index"


@dataclass(frozen=True)
class MemorySettings:
    """How the package indexes and recalls: cadence, scope, and the models.

    ``ambient_n`` — the ambient recall hook fires every Nth owner turn.
    ``window`` — transcript messages the judge sees. ``top_k`` — candidates
    pre-fetched per query. ``injection_cap_tokens`` — recall injection ceiling.
    ``include``/``exclude`` — vault-relative path prefixes gating what indexes.
    ``vault_paths`` — the vault root(s). ``writable_paths`` — where the agent
    may save notes (capability follows config; empty means read-only).
    """

    ambient_n: int = 5
    window: int = 30
    top_k: int = 8
    injection_cap_tokens: int = 1500
    judge_role: str = "memory_judge"
    embed_model: str = "minishlab/potion-base-8M"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = _DEFAULT_EXCLUDE
    vault_paths: tuple[str, ...] = ()
    writable_paths: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, mapping: Mapping[str, Any] | None) -> "MemorySettings":
        """Build settings from the sliced ``obsidian_memory`` dict, defaulting
        every missing sub-key and coercing list-valued keys to tuples."""
        raw = dict(mapping or {})
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in known:
                continue
            kwargs[key] = tuple(value) if key in _TUPLE_KEYS else value
        return cls(**kwargs)
