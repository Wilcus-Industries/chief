"""Chief-owned permission-gate + hook vocabulary (#88).

The gate (:mod:`chief.gate.gate`) is SDK-agnostic: it rules on a ``(tool_name,
tool_input)`` pair and returns an allow/deny/ask verdict, and its two callbacks are
adapted onto whichever agent SDK is live. That contract used to be *spelled* in
claude-agent-sdk's own types (``CanUseTool``, ``PermissionResultAllow`` / ``…Deny``,
``ToolPermissionContext``, ``HookMatcher``, ``HookCallback`` / ``HookContext`` /
``HookEvent``). With that SDK removed, chief owns the vocabulary here — a faithful
mirror of only the members chief actually uses, so the gate stays SDK-neutral and
:mod:`chief.core.copilot_gate` remains the single boundary that maps it onto the
Copilot SDK's permission + hook surface.

The permission-result and context shapes match claude-agent-sdk's field names so the
adapter and the tests read the same. The hook types are deliberately *loose* — a
``PreToolUse`` / ``PostToolUse`` hook reads and returns the runtime ``dict`` shapes the
adapter drives it with (there is no strict per-event input union to satisfy anymore),
so the boundary ``cast``s the gate/screening hooks kept for documentation intent still
type-check cleanly.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

#: The hook events chief binds. The gate's fast classify+audit pass runs on
#: ``PreToolUse``; injection screening + screenshot delivery run on ``PostToolUse``.
HookEvent = Literal["PreToolUse", "PostToolUse"]


class HookContext(TypedDict):
    """Context passed to a hook callback. ``signal`` is a reserved abort slot (None)."""

    signal: Any | None


#: A hook callback: ``(input_data, tool_use_id, context) -> output``. Loose ``dict``
#: shapes both ways — the adapter (:mod:`chief.core.copilot_gate`) drives it with the
#: runtime dicts and reads the runtime dict back.
HookCallback = Callable[
    [dict[str, Any], str | None, HookContext], Awaitable[dict[str, Any]]
]


@dataclass
class HookMatcher:
    """A group of hook callbacks bound for one event (``matcher`` unused by chief)."""

    matcher: str | None = None
    hooks: list[HookCallback] = field(default_factory=list)


@dataclass
class ToolPermissionContext:
    """Context handed to a ``can_use_tool`` callback (per-call metadata).

    chief's gate reads none of these fields — the ``(tool_name, tool_input)`` pair
    carries everything it classifies on — but the type is the callback's third
    parameter, so it is mirrored (default-constructible) for the adapter and the tests.
    """

    signal: Any | None = None
    tool_use_id: str | None = None


@dataclass
class PermissionResultAllow:
    """The gate allowed the call. ``behavior`` mirrors claude-agent-sdk's tag."""

    behavior: Literal["allow"] = "allow"


@dataclass
class PermissionResultDeny:
    """The gate denied the call; ``message`` is the reason surfaced to the model."""

    behavior: Literal["deny"] = "deny"
    message: str = ""


PermissionResult = PermissionResultAllow | PermissionResultDeny

#: The ``can_use_tool`` callback shape: ``(tool_name, tool_input, context) -> result``.
CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResult]
]


class McpHttpServerConfig(TypedDict):
    """A streamable-HTTP MCP server entry (the Google/browser containers)."""

    type: Literal["http"]
    url: str
    headers: NotRequired[dict[str, str]]
