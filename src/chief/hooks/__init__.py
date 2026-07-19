"""Agent-loop hooks: package-contributed context providers and turn observers.

A package ships a ``hooks`` module whose ``register(context, hooks)`` wires its
callables into the central :class:`HookRegistry`. Context hooks (pre_turn,
session_start) add name-attributed blocks to the system prompt; observers
(post_turn) watch finished turns. Everything a hook may touch is the
:class:`HookContext`; execution is resilient (:mod:`chief.hooks.runner`).
"""

from chief.hooks.context import HookContext, TurnContext
from chief.hooks.registry import (
    HookRegistry,
    PackageHookRegistrar,
    PostTurnHook,
    PreTurnHook,
    SessionStartHook,
)
from chief.hooks.runner import (
    assemble_system,
    render_block,
    run_context_hooks,
    run_post_turn,
)

__all__ = [
    "HookContext",
    "HookRegistry",
    "PackageHookRegistrar",
    "PostTurnHook",
    "PreTurnHook",
    "SessionStartHook",
    "TurnContext",
    "assemble_system",
    "render_block",
    "run_context_hooks",
    "run_post_turn",
]
