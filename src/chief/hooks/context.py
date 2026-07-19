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


@dataclass(frozen=True)
class TurnContext:
    """What a context hook sees about the turn it is contributing to.

    Built once per turn and passed to every ``pre_turn``/``session_start``
    hook. ``messages`` is the recent transcript *before* the incoming user
    turn is appended, so a hook can judge relevance against the conversation
    so far. ``sender`` is the inbound message's origin (``"owner"``,
    ``"system"`` for monitor/cron wakes, or a stranger id) — hooks that touch
    private data gate on it. ``channel`` is None for wakes without a channel.
    """

    user_text: str
    messages: list[dict[str, Any]]
    sender: str
    thread_key: str
    channel: str | None
