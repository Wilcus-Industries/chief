"""HookContext: the entire surface a package hook may touch.

A hook is handed exactly this — its provider, the model roster, its own sliced
config, a private data dir, a package-scoped logger, and the budget. There is
deliberately no access to sessions, the gate, or the self-edit pipeline: hooks
contribute per-turn context and observe finished turns, they do not steer the
daemon.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chief.budget import Budget
from chief.provider.base import Provider


@dataclass(frozen=True)
class HookContext:
    """Immutable per-package handle passed to a package's ``register``."""

    provider: Provider
    models: Mapping[str, str]
    config: Mapping[str, Any]
    data_dir: Path
    logger: logging.Logger
    budget: Budget
