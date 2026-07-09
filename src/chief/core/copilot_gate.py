"""Map chief's permission gate onto the Copilot SDK's permission surface (#77, #72).

chief's gate (:mod:`chief.gate.gate`) is SDK-agnostic: :func:`~chief.gate.gate.classify`
rules on a ``(tool_name, tool_input)`` pair, :func:`~chief.gate.gate.build_can_use_tool`
owns the ASK → approval-card round-trip, and :func:`~chief.gate.gate.build_pretool_hook`
is the fast classify+audit pass. The claude-agent-sdk backend feeds those two callbacks
straight to the SDK. The Copilot SDK exposes the *same two seams* under different names
and shapes, so this module is the thin boundary adapter between them.

**The two seams.**

* ``on_permission_request`` (:data:`PermissionHandlerFn`) is Copilot's interactive
  approval callback — the analogue of ``can_use_tool``. Its argument is a **kind-tagged
  union** (:data:`~copilot.PermissionRequest`: read / write / shell / mcp / custom-tool
  / url / …), each variant carrying heterogeneous fields and *no* generic
  ``(tool_name, tool_input)`` pair. :func:`normalize_permission_request` folds each kind
  into what :func:`classify` reads; :func:`build_permission_handler` then hands off to
  chief's ``can_use_tool`` (which raises the card on ASK and blocks) and maps its
  allow/deny result onto Copilot's decision vocabulary. This is the **authoritative**
  gate: it governs every kind, including ``custom-tool`` (in-process ``@define_tool``
  calls), so a custom tool is gated even if the pre-tool hook never fires for it.
* ``on_pre_tool_use`` (:data:`~copilot.PreToolUseHandler`, inside
  :data:`~copilot.SessionHooks`) is Copilot's ``PreToolUse`` hook. Its output
  ``permissionDecision: "allow"|"deny"|"ask"`` is structurally identical to chief's hook
  output, so :func:`build_session_hooks` is a near-drop-in that delegates to chief's
  hook. It fires with Copilot's *native* tool names (which chief may not recognise), so
  it acts as a fast hard-deny + audit + "ask = defer to the handler" pass; the precise
  per-kind decision is made in ``on_permission_request``.

**Decision-vocabulary mapping.** chief owns its own persisted allowlist
(:class:`~chief.gate.policy.PolicyStore`) and audit trail, so it must stay the single
source of truth. Every allow is therefore mapped to
:class:`PermissionDecisionApproveOnce`
(per call), never Copilot's ``approve-for-session`` / ``approve-for-location`` /
``approve-permanently``: a "⭐ always allow" tap writes a chief PolicyStore rule, so the
*next* identical call re-enters this handler, chief re-classifies it as APPROVED, and
returns ``approve-once`` again with no card. Delegating memory to Copilot instead would
bypass chief's audit trail and let a later "🚫 always deny" fail to re-block within the
session. ``approve-for-location`` has no chief analogue (chief's list is global, not
per-location) and ``approve-permanently`` is URL-domain-only in the SDK — both dropped.
A deny maps to :class:`PermissionDecisionReject` carrying chief's reason.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from claude_agent_sdk import (
    CanUseTool,
    HookMatcher,
    PermissionResultDeny,
    ToolPermissionContext,
)
from claude_agent_sdk.types import HookContext, HookEvent
from copilot import (
    PermissionRequest,
    PermissionRequestResult,
    SessionHooks,
)

# The concrete PermissionRequest kind classes and PermissionDecision variants are not
# re-exported from the top-level ``copilot`` package; import them from the generated
# (``py.typed``) modules that define the SDK's public wire types.
from copilot.generated.rpc import (
    PermissionDecisionApproveOnce,
    PermissionDecisionReject,
)
from copilot.generated.session_events import (
    PermissionRequestCustomTool,
    PermissionRequestMcp,
    PermissionRequestRead,
    PermissionRequestShell,
    PermissionRequestUrl,
    PermissionRequestWrite,
)

logger = logging.getLogger("chief.core.copilot_gate")

#: chief's sandbox-shell command-tool name. Copilot's native ``shell`` permission kind
#: normalizes to it so the command string flows through the same ``COMMAND_TOOLS``
#: safe-match (and "Run: …" approval preview) as the claude-agent-sdk path, and so an
#: un-approved shell command falls through :func:`classify` to ASK — never a hard DENY
#: (the built-in ``Bash`` hard-DENY is specific to the claude-agent-sdk in-core shell,
#: which reaches the Max OAuth token; Copilot's shell runs in the separate runtime).
COPILOT_SHELL_TOOL = "mcp__chief_shell__bash"

#: The ``on_permission_request`` callback shape: a kind-tagged request plus the SDK's
#: per-invocation metadata, resolved to a Copilot permission decision.
PermissionHandlerFn = Callable[
    [PermissionRequest, dict[str, str]],
    Awaitable[PermissionRequestResult],
]


def _as_dict(args: Any) -> dict[str, Any]:
    """Coerce a request's free-form ``args`` payload to a dict for ``classify``."""
    return args if isinstance(args, dict) else {}


def normalize_permission_request(
    request: PermissionRequest,
) -> tuple[str, dict[str, Any]]:
    """Fold a kind-tagged :data:`PermissionRequest` into ``(tool_name, tool_input)``.

    The tuple is what :func:`~chief.gate.gate.classify` reads, so each Copilot kind is
    mapped onto the chief tool vocabulary that drives the right verdict:

    - ``read`` → ``("Read", {"file_path": path})`` — chief's file-op confinement applies
      (owner: allowed within memory ∪ workspace; guest with no roots: DENY).
    - ``write`` → ``("Write", {"file_path": file_name})`` — same rule (guest: DENY).
    - ``shell`` → ``(COPILOT_SHELL_TOOL, {"command": full_command_text})`` — routed to
      the command-tool safe-match, so it ASKs unless pre-approved.
    - ``mcp`` / ``custom-tool`` → ``(tool_name, args)`` verbatim — chief classifies by
      the real tool name (unknown/effectful → ASK); the ``@define_tool`` gating path.
    - ``url`` → ``("url", {"url": url})`` — no chief read-only analogue, so it ASKs.

    Any other kind (memory / hook / extension-*) has no chief analogue and falls through
    to its ``kind`` string as the tool name, so :func:`classify` returns ASK (fail-safe:
    the owner decides).
    """
    if isinstance(request, PermissionRequestRead):
        return "Read", {"file_path": request.path}
    if isinstance(request, PermissionRequestWrite):
        return "Write", {"file_path": request.file_name}
    if isinstance(request, PermissionRequestShell):
        return COPILOT_SHELL_TOOL, {"command": request.full_command_text}
    if isinstance(request, (PermissionRequestMcp, PermissionRequestCustomTool)):
        return request.tool_name, _as_dict(request.args)
    if isinstance(request, PermissionRequestUrl):
        return "url", {"url": request.url}
    return request.kind, {}


def build_permission_handler(can_use_tool: CanUseTool) -> PermissionHandlerFn:
    """Adapt chief's ``can_use_tool`` into Copilot's ``on_permission_request`` callback.

    Normalizes the kind-tagged request, delegates the allow/deny/ASK-card decision to
    the real gate, and maps the result onto Copilot's decision vocabulary
    (:class:`PermissionDecisionApproveOnce` / :class:`PermissionDecisionReject`; see the
    module docstring for why never ``approve-for-session``).
    """

    async def on_permission_request(
        request: PermissionRequest, invocation: dict[str, str]
    ) -> PermissionRequestResult:
        tool_name, tool_input = normalize_permission_request(request)
        result = await can_use_tool(tool_name, tool_input, ToolPermissionContext())
        if isinstance(result, PermissionResultDeny):
            return PermissionDecisionReject(feedback=result.message or None)
        return PermissionDecisionApproveOnce()

    return on_permission_request


# The chief ``PreToolUse`` hook is typed against the SDK's strict hook-input/output
# unions; this adapter drives it with the runtime dict shapes, so cast to the loose
# runtime signature at the call boundary (same runtime shape, see build_pretool_hook).
_LooseHook = Callable[[dict[str, Any], str | None, Any], Awaitable[dict[str, Any]]]

#: Deny is the safest, ask defers to the handler, allow is least restrictive — so when
#: several hooks rule, the most restrictive decision wins.
_DECISION_PRECEDENCE = ("deny", "ask", "allow")


def _pretool_hooks(hooks: dict[HookEvent, list[HookMatcher]]) -> list[_LooseHook]:
    """The flat list of ``PreToolUse`` hook callbacks bound in the SDK ``hooks`` map."""
    return [
        cast(_LooseHook, hook)
        for matcher in hooks.get("PreToolUse", [])
        for hook in matcher.hooks
    ]


def build_session_hooks(
    hooks: dict[HookEvent, list[HookMatcher]],
) -> SessionHooks | None:
    """Wrap chief's ``PreToolUse`` hook(s) as a Copilot :data:`SessionHooks` map.

    Returns ``None`` when no ``PreToolUse`` hook is bound, so the caller passes no hooks
    to the SDK rather than an empty handler. The wrapped handler classifies + audits
    every call (chief's hook writes the audit line) and returns the fast allow/deny/ask
    verdict; ``ask`` defers the real decision to ``on_permission_request``.
    """
    pretool = _pretool_hooks(hooks)
    if not pretool:
        return None

    async def on_pre_tool_use(
        hook_input: dict[str, Any], invocation: dict[str, str]
    ) -> dict[str, Any] | None:
        chief_input = {
            "tool_name": hook_input.get("toolName", ""),
            "tool_input": hook_input.get("toolArgs") or {},
        }
        decisions: dict[str, str] = {}
        for hook in pretool:
            output = await hook(chief_input, None, HookContext(signal=None))
            spec = output.get("hookSpecificOutput", {})
            decision = spec.get("permissionDecision")
            if decision:
                decisions[decision] = spec.get("permissionDecisionReason", "")
        for decision in _DECISION_PRECEDENCE:
            if decision in decisions:
                return {
                    "permissionDecision": decision,
                    "permissionDecisionReason": decisions[decision],
                }
        return None

    return {"on_pre_tool_use": cast(Any, on_pre_tool_use)}
