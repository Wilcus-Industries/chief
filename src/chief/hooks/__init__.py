"""Agent-loop hooks: package-contributed context providers and turn observers.

A package ships a ``hooks`` module whose ``register(context, hooks)`` wires its
callables into the central :class:`HookRegistry`. Context hooks (pre_turn,
session_start) add name-attributed blocks to the system prompt; observers
(post_turn) watch finished turns. A ``post_tool`` hook stands between a tool
returning and the model reading it — it may annotate or veto, never rewrite.
Everything a hook may touch is the :class:`HookContext`; execution is
resilient (:mod:`chief.hooks.runner`).
"""

from chief.hooks.context import HookContext, TurnContext
from chief.hooks.posttool import (
    Annotate,
    PostToolHook,
    PostToolVerdict,
    Veto,
    run_post_tool,
    tool_screener,
)
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
    "Annotate",
    "HookContext",
    "HookRegistry",
    "PackageHookRegistrar",
    "PostToolHook",
    "PostToolVerdict",
    "PostTurnHook",
    "PreTurnHook",
    "SessionStartHook",
    "TurnContext",
    "Veto",
    "assemble_system",
    "render_block",
    "run_context_hooks",
    "run_post_tool",
    "run_post_turn",
    "tool_screener",
]
