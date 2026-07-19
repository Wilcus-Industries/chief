"""Boot loader: discover installed packages that declare hooks and register them.

Each package's ``hooks.module`` is imported by file path and its ``register``
entry point is called with a per-package :class:`HookContext` and a
:class:`PackageHookRegistrar`. A package whose entry point fails to import (or
raises during registration) is skipped with a loud ERROR — the owner's notice —
so one broken package can never block boot.
"""

import importlib.util
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from chief.budget import Budget
from chief.hooks.context import HookContext
from chief.hooks.registry import HookRegistry, PackageHookRegistrar
from chief.hooks.runner import is_safe_package_name
from chief.packages import HookSpec, PackageLibrary
from chief.provider.base import Provider

logger = logging.getLogger(__name__)


def load_hooks(
    *,
    library: PackageLibrary,
    installed: Mapping[str, Any],
    registry: HookRegistry,
    provider: Provider,
    models: Mapping[str, str],
    budget: Budget,
    raw_config: Mapping[str, Any],
    disabled: tuple[str, ...],
    data_root: Path,
) -> None:
    """Register the hooks of every installed, non-disabled package that declares
    them. Errors are contained per-package; the registry gains what loads."""
    for pkg in library.scan():
        if pkg.name not in installed or pkg.name in disabled or pkg.hooks is None:
            if (
                pkg.hooks is not None
                and pkg.name not in installed
                and pkg.name not in disabled
                and _looks_half_installed(pkg.skills)
            ):
                # Its skills landed but the registry entry didn't: a
                # half-install. Silent skipping is how one lingered in prod —
                # the package "worked" (skills answered) while its hooks never
                # loaded and discovery called it uninstalled.
                logger.warning(
                    "package %s has installed skills but no data/installed.yaml "
                    "entry — its hooks are NOT loaded. Finish the install: "
                    "uv run python -m chief.registry_apply %s",
                    pkg.name,
                    pkg.name,
                )
            continue
        if not is_safe_package_name(pkg.name):
            logger.error(
                "package %r has an unsafe name; skipping its hooks", pkg.name
            )
            continue
        try:
            _register_package(
                pkg.name, pkg.path / pkg.hooks.module, pkg.hooks,
                registry, _context(pkg.name, pkg.config_keys, raw_config,
                                   provider, models, budget, data_root),
            )
        except Exception:
            logger.error(
                "package %s hooks failed to load; skipping", pkg.name, exc_info=True
            )


def _looks_half_installed(
    skills: tuple[str, ...], skills_root: Path = Path("skills")
) -> bool:
    return any((skills_root / skill / "SKILL.md").exists() for skill in skills)


def _register_package(
    name: str, module_path: Path, spec: HookSpec,
    registry: HookRegistry, context: HookContext,
) -> None:
    loaded = importlib.util.spec_from_file_location(f"chief_hook_{name}", module_path)
    if loaded is None or loaded.loader is None:
        raise ImportError(f"cannot load hook module at {module_path}")
    module = importlib.util.module_from_spec(loaded)
    loaded.loader.exec_module(module)
    register = getattr(module, spec.register)
    register(context, PackageHookRegistrar(registry, name))


def _context(
    name: str, config_keys: tuple[str, ...], raw_config: Mapping[str, Any],
    provider: Provider, models: Mapping[str, str], budget: Budget, data_root: Path,
) -> HookContext:
    data_dir = data_root / name
    data_dir.mkdir(parents=True, exist_ok=True)
    return HookContext(
        provider=provider,
        models=models,
        config={k: raw_config[k] for k in config_keys if k in raw_config},
        data_dir=data_dir,
        logger=logging.getLogger(f"chief.hooks.{name}"),
        budget=budget,
    )
