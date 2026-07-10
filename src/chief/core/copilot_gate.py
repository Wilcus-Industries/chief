"""Map chief's permission gate onto the Copilot SDK's permission surface (#77, #72).

chief's gate (:mod:`chief.gate.gate`) is SDK-agnostic: :func:`~chief.gate.gate.classify`
rules on a ``(tool_name, tool_input)`` pair, :func:`~chief.gate.gate.build_can_use_tool`
owns the ASK → approval-card round-trip, and :func:`~chief.gate.gate.build_pretool_hook`
is the fast classify+audit pass. Those callbacks are spelled in chief's own SDK-neutral
vocabulary (:mod:`chief.gate.types`); the Copilot SDK exposes the *same two seams* under
different names and shapes, so this module is the thin boundary adapter between them.

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
* ``on_post_tool_use`` (:data:`~copilot.PostToolUseHandler`, also inside
  :data:`~copilot.SessionHooks`) is Copilot's ``PostToolUse`` hook, running *after* a
  tool returns — the seam chief's untrusted-content injection screening
  (:func:`chief.core.screening.build_screening_hook`) and screenshot delivery hang off.
  :func:`build_session_hooks` forwards chief's ``PostToolUse`` hook(s) here too;
  :func:`_adapt_post_output` bridges chief's block / ``additionalContext`` output onto
  the SDK's ``PostToolUseHookOutput`` (which has no ``block`` field — a block becomes a
  ``modifiedResult`` that replaces the flagged content). Dropping this (the pre-#95 bug)
  makes host-native screening a silent no-op on the Copilot backend. The SDK routes a
  result it classifies as a *failure* (``isError`` true) to a **separate**
  ``on_post_tool_use_failure`` hook, not this one, so :func:`build_session_hooks`
  forwards the same screening pass there as well (:func:`_build_post_failure_handler`) —
  otherwise a flagged *failed* result (e.g. an external browser tool returning
  attacker-controlled page text inside an error) would skip screening entirely (#96).
  That output carries only ``additionalContext`` (no ``modifiedResult`` / block
  channel), so the failure path can annotate but not replace.

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

from ..gate.types import (
    CanUseTool,
    HookContext,
    HookEvent,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

logger = logging.getLogger("chief.core.copilot_gate")

#: chief's host-shell command-tool name. Copilot's native ``shell`` permission kind
#: normalizes to it so the command string flows through the same ``COMMAND_TOOLS``
#: safe-match (and "Run: …" approval preview) as chief's own ``bash`` tool, and so an
#: un-approved shell command falls through :func:`classify` to ASK — never a hard DENY.
#: (The built-in ``Bash`` hard-DENY names a separate SDK built-in chief keeps disabled
#: so it can't run as a second, un-blacklisted shell beside the per-task host shell.)
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


def _qualify_mcp(server_name: str, tool_name: str) -> str:
    """Rebuild the ``mcp__<server>__<tool>`` name chief's gate keys off.

    The Copilot SDK delivers an MCP tool call as a *split* ``server_name`` + bare
    ``tool_name`` (the two are separate fields on
    :class:`~copilot.generated.session_events.PermissionRequestMcp`), so they are joined
    into the qualified name chief's allowlists / ``COMMAND_TOOLS`` / blacklist all use.
    Defensive: a ``tool_name`` already carrying the ``mcp__`` prefix is passed through
    rather than double-qualified, so the mapping is correct whether the runtime sends
    the bare or the pre-qualified form.
    """
    if tool_name.startswith("mcp__"):
        return tool_name
    return f"mcp__{server_name}__{tool_name}"


def normalize_permission_request(
    request: PermissionRequest,
) -> tuple[str, dict[str, Any]]:
    """Fold a kind-tagged :data:`PermissionRequest` into ``(tool_name, tool_input)``.

    The tuple is what :func:`~chief.gate.gate.classify` reads, so each Copilot kind is
    mapped onto the chief tool vocabulary that drives the right verdict:

    - ``read`` → ``("Read", {"file_path": path})`` — owner reads are unconfined at the
      ``classify()`` layer (no ``file_path`` check); a guest never reaches ``Read`` at
      all, since it's in ``GUEST_DENIED`` (``disallowed_tools`` at the SDK layer, per
      ``tasks.py``'s ``GUEST_DENIED = sorted(set(MEMORY_TOOLS) |
      set(WORKSPACE_TOOLS))``) — not because of a path fence.
    - ``write`` → ``("Write", {"file_path": file_name})`` — same rule: owner writes are
      unconfined at ``classify()``; a guest never reaches ``Write`` (also in
      ``GUEST_DENIED``).
    - ``shell`` → ``(COPILOT_SHELL_TOOL, {"command": full_command_text})`` — routed to
      the command-tool safe-match, so it ASKs unless pre-approved.
    - ``mcp`` → ``("mcp__<server>__<tool>", args)`` — the SDK carries the MCP tool as
      *split* ``server_name`` + bare ``tool_name`` fields, so they are re-joined into
      the ``mcp__<server>__<tool>`` name chief's allowlists / blacklist key off (the
      Google/browser HTTP-server gating path). A bare name here would silently un-gate.
    - ``custom-tool`` → ``(tool_name, args)`` verbatim — chief's in-process
      shell/scheduler/guest tools are already registered under their SDK-qualified
      ``mcp__<server>__<tool>`` names (see :mod:`chief.core.copilot_tools`), so the
      custom-tool name arrives pre-qualified and matches chief's vocabulary directly.
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
    if isinstance(request, PermissionRequestMcp):
        return _qualify_mcp(request.server_name, request.tool_name), _as_dict(
            request.args
        )
    if isinstance(request, PermissionRequestCustomTool):
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

    Only an explicit :class:`PermissionResultAllow` approves — a deny **or any
    unrecognized result type** rejects (fail closed). A gate that returns something the
    contract doesn't cover must never coast through as an approval.
    """

    async def on_permission_request(
        request: PermissionRequest, invocation: dict[str, str]
    ) -> PermissionRequestResult:
        tool_name, tool_input = normalize_permission_request(request)
        result = await can_use_tool(tool_name, tool_input, ToolPermissionContext())
        if isinstance(result, PermissionResultAllow):
            return PermissionDecisionApproveOnce()
        if isinstance(result, PermissionResultDeny):
            return PermissionDecisionReject(feedback=result.message or None)
        # Unknown result type — reject rather than let it pass as an approval.
        logger.warning(
            "gate returned unrecognized result %r for %s; rejecting",
            type(result).__name__,
            tool_name,
        )
        return PermissionDecisionReject(feedback="unrecognized gate result")

    return on_permission_request


# The chief ``PreToolUse`` hook is typed against the SDK's strict hook-input/output
# unions; this adapter drives it with the runtime dict shapes, so cast to the loose
# runtime signature at the call boundary (same runtime shape, see build_pretool_hook).
_LooseHook = Callable[[dict[str, Any], str | None, Any], Awaitable[dict[str, Any]]]

#: Deny is the safest, ask defers to the handler, allow is least restrictive — so when
#: several hooks rule, the most restrictive decision wins.
_DECISION_PRECEDENCE = ("deny", "ask", "allow")


#: The Copilot session-hook callback shape both adapters produce: a runtime input dict
#: plus the SDK's per-invocation metadata, resolved to a Copilot hook-output dict (or
#: ``None`` for "no change"). Cast to :data:`SessionHooks`' strict per-hook types at the
#: map boundary — same runtime shape, see ``build_session_hooks``.
_SessionHookFn = Callable[
    [dict[str, Any], dict[str, str]], Awaitable[dict[str, Any] | None]
]


def _pretool_hooks(hooks: dict[HookEvent, list[HookMatcher]]) -> list[_LooseHook]:
    """The flat list of ``PreToolUse`` hook callbacks bound in the SDK ``hooks`` map."""
    return [
        cast(_LooseHook, hook)
        for matcher in hooks.get("PreToolUse", [])
        for hook in matcher.hooks
    ]


def _posttool_hooks(hooks: dict[HookEvent, list[HookMatcher]]) -> list[_LooseHook]:
    """The flat list of ``PostToolUse`` hook callbacks bound in the SDK hooks map."""
    return [
        cast(_LooseHook, hook)
        for matcher in hooks.get("PostToolUse", [])
        for hook in matcher.hooks
    ]


def _build_pre_handler(pretool: list[_LooseHook]) -> _SessionHookFn:
    """The ``on_pre_tool_use`` callback: run chief's ``PreToolUse`` hook(s), most-
    restrictive decision wins (:data:`_DECISION_PRECEDENCE`); ``ask`` defers to
    ``on_permission_request``."""

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

    return on_pre_tool_use


def _post_annotation(chief_output: dict[str, Any]) -> tuple[str | None, bool]:
    """Extract ``(warning_text, is_block)`` from a chief ``PostToolUse`` hook's output.

    chief's screening hook returns a **block** decision (``{"decision": "block",
    "reason": …}``), an ``additionalContext`` **annotation**
    (``{"hookSpecificOutput": {"additionalContext": …}}``), or ``{}`` when the result
    passes clean; the screenshot-delivery hook returns only the empty envelope (its work
    is a ``send_file`` side effect). This is the shared read of that output both the
    success- and failure-path adapters key off, so the two can't drift on how a flag is
    recognised. A clean/empty output yields ``(None, False)``.
    """
    if not chief_output:
        return None, False
    if chief_output.get("decision") == "block":
        return chief_output.get("reason", ""), True
    spec = chief_output.get("hookSpecificOutput", {})
    context = spec.get("additionalContext")
    if context:
        return context, False
    return None, False


def _adapt_post_output(chief_output: dict[str, Any]) -> dict[str, Any]:
    """Map a chief ``PostToolUse`` hook's output onto ``PostToolUseHookOutput``.

    The Copilot post-hook output has **no** ``block`` field, so a block is expressed as
    a ``modifiedResult`` that REPLACES the flagged result — the model never reads the
    injection content — with the same warning also as ``additionalContext`` so the block
    reason is explicit. An annotation maps to ``additionalContext`` (the result is still
    delivered, warning appended). A clean or empty result maps to ``{}`` (no change).
    """
    text, is_block = _post_annotation(chief_output)
    if text is None:
        return {}
    if is_block:
        return {"modifiedResult": text, "additionalContext": text}
    return {"additionalContext": text}


def _adapt_post_failure_output(chief_output: dict[str, Any]) -> dict[str, Any]:
    """Map a chief ``PostToolUse`` hook's output onto ``PostToolUseFailureHookOutput``.

    The failure output (verified against the installed SDK) carries **only**
    ``additionalContext`` — no ``modifiedResult`` and no block/decision channel — so a
    flag can only ANNOTATE the failed result, matching the ``screening_block=False``
    default. Even a ``block`` decision degrades to an annotation here: its reason is
    surfaced as ``additionalContext`` so the model is still warned, though the content
    can't be replaced. A clean or empty result maps to ``{}`` (no change).
    """
    text, _is_block = _post_annotation(chief_output)
    if text is None:
        return {}
    return {"additionalContext": text}


async def _run_post_hooks(
    posttool: list[_LooseHook],
    chief_input: dict[str, Any],
    adapt: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any] | None:
    """Run chief's ``PostToolUse`` hook(s) on one remapped input, merging each one's
    ``adapt``-ed output into a single Copilot output.

    Returns ``None`` when nothing was annotated or blocked, so the SDK leaves the result
    untouched. Shared by the success- and failure-path handlers so their fan-out/merge
    logic can't drift.
    """
    output: dict[str, Any] = {}
    for hook in posttool:
        result = await hook(chief_input, None, HookContext(signal=None))
        output.update(adapt(result))
    return output or None


def _build_post_handler(posttool: list[_LooseHook]) -> _SessionHookFn:
    """The ``on_post_tool_use`` callback: run chief's ``PostToolUse`` hook(s) on a
    successful tool result.

    chief's ``PostToolUse`` input shape is ``(tool_name, tool_response)`` — the Copilot
    ``toolName`` / ``toolResult`` fields are remapped onto it, just as the pre adapter
    remaps ``toolName`` / ``toolArgs``.
    """

    async def on_post_tool_use(
        hook_input: dict[str, Any], invocation: dict[str, str]
    ) -> dict[str, Any] | None:
        chief_input = {
            "tool_name": hook_input.get("toolName", ""),
            "tool_response": hook_input.get("toolResult"),
        }
        return await _run_post_hooks(posttool, chief_input, _adapt_post_output)

    return on_post_tool_use


def _build_post_failure_handler(posttool: list[_LooseHook]) -> _SessionHookFn:
    """The ``on_post_tool_use_failure`` callback: run chief's ``PostToolUse`` screening
    hook(s) on a tool result the SDK classified as a **failure** (``isError`` true).

    The Copilot runtime routes a failed tool result to ``postToolUseFailure`` — *not*
    ``postToolUse`` — passing only the extracted ``error`` string (``copilot/tools.py``
    sets ``result_type="failure"`` on ``isError``). Without this seam, chief's injection
    screening skips every failed result, so attacker-controlled page text returned in an
    external browser tool's error would reach the model unscreened (#96). chief's
    screening hook reads ``(tool_name, tool_response)``; the failure input's
    ``toolName`` / ``error`` fields are remapped onto it. ``toolName`` is fully
    qualified — the same field the success hook receives, and the failure input carries
    no ``serverName`` — so the exact-name keying in
    :func:`chief.core.screening.build_screening_hook` matches with no requalification.
    """

    async def on_post_tool_use_failure(
        hook_input: dict[str, Any], invocation: dict[str, str]
    ) -> dict[str, Any] | None:
        chief_input = {
            "tool_name": hook_input.get("toolName", ""),
            "tool_response": hook_input.get("error"),
        }
        return await _run_post_hooks(
            posttool, chief_input, _adapt_post_failure_output
        )

    return on_post_tool_use_failure


def build_session_hooks(
    hooks: dict[HookEvent, list[HookMatcher]],
) -> SessionHooks | None:
    """Wrap chief's ``PreToolUse`` + ``PostToolUse`` hook(s) as a :data:`SessionHooks`.

    Returns ``None`` only when *neither* a pre- nor a post-tool hook is bound, so the
    caller passes no hooks to the SDK rather than an empty handler — a map is built
    whenever *either* kind is present (so a post-only gate is honoured, not dropped).

    * ``on_pre_tool_use`` (when a ``PreToolUse`` hook is bound) classifies + audits each
      call and returns the fast allow/deny/ask verdict; ``ask`` defers the real decision
      to ``on_permission_request``.
    * ``on_post_tool_use`` (when a ``PostToolUse`` hook is bound) runs chief's real
      result-screening (:func:`chief.core.screening.build_screening_hook`) and
      screenshot-delivery hooks on the tool result — the host-native injection boundary,
      which is a no-op on this backend if the post hooks are dropped at the adapter.
    * ``on_post_tool_use_failure`` (wired alongside ``on_post_tool_use``, from the same
      ``PostToolUse`` hook) runs that screening on a result the SDK classifies as a
      *failure* (``isError`` true), which the runtime routes to the failure hook and
      *not* ``on_post_tool_use`` — so a flagged failed result is screened too, rather
      than reaching the model unscreened (#96).
    """
    pretool = _pretool_hooks(hooks)
    posttool = _posttool_hooks(hooks)
    if not pretool and not posttool:
        return None
    session_hooks: SessionHooks = {}
    if pretool:
        session_hooks["on_pre_tool_use"] = cast(Any, _build_pre_handler(pretool))
    if posttool:
        session_hooks["on_post_tool_use"] = cast(Any, _build_post_handler(posttool))
        session_hooks["on_post_tool_use_failure"] = cast(
            Any, _build_post_failure_handler(posttool)
        )
    return session_hooks
