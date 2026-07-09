"""Cheap classifier judgments used by the task engine.

Two shapes, split by cost (#88, part of #72):

- **Text one-shots** — ``ask_yes_no`` (``stop_intent`` / ``warrants_task`` /
  ``is_complex`` / and, via :mod:`chief.core.screening`, ``screen_text``) and
  ``classify_category`` — each fire per message / per tool result. They run as a
  **direct OpenRouter chat-completions HTTP call** (httpx) on a fixed cheap model, never
  a
  Copilot session: a Copilot turn would burn the 200/mo premium-request cap on every
  keystroke. All fail **safe** — any error, a non-2xx, or a missing key returns the
  cautious default (``False`` / the fallback category): no interrupt, no spawn, no
  escalation, content passes unscreened. When the OpenRouter key is absent, **no HTTP
  call is made at all** (the classifier short-circuits to its safe default); the boot
  warns once (:func:`chief.app.warn_if_classifier_keyless`) so the degradation is
  deliberate, not silent.

- ``ask_condition`` — the scheduler's agent monitors need to *look something up* before
  judging (web + the owner's Google reads), so it runs an **ephemeral Copilot session**
  with a ``can_use_tool`` gate that allow-lists exactly the read tools it is handed and
  denies everything else. It too fails safe to ``False`` — a monitor must never flip on
  an evaluation error.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from ..gate.types import (
    CanUseTool,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)
from .backend import CopilotBackend
from .copilot_session import OPENROUTER_BASE_URL
from .session import Final, SessionProto

logger = logging.getLogger("chief.core.classify")

#: Per-request timeout for a classifier HTTP call (seconds). A cheap one-shot answers
#: fast; a slow provider fails safe rather than stalling a message's steering check.
_CLASSIFIER_TIMEOUT = 30.0
#: Token cap for a classifier reply — a bare YES/NO or a single category name is tiny,
#: so bound the spend hard. Kept generous enough for a one-word category.
_MAX_TOKENS = 16

#: Wall-clock bound (seconds) on one :func:`ask_condition` monitor evaluation. The
#: evaluation is awaited inline in the scheduler tick loop, so a wedged Copilot turn
#: must never block all scheduling. The old claude-backend ``max_turns=4`` bound has no
#: Copilot equivalent (``CopilotBackend.create_session`` exposes none), so this
#: timeout is the bound. Timeout → fail-safe ``False``.
_CONDITION_TIMEOUT = 120.0

#: Wall-clock bound (seconds) on the teardown that follows a monitor turn. On the
#: timeout path the session is wedged by definition, and ``CopilotSession.aclose``
#: awaits an unbounded ``disconnect``/``stop``; a hang there would re-block the same
#: tick loop the turn timeout just rescued. Mirrors
#: :data:`chief.core.tasks._RESET_TIMEOUT`.
_CLOSE_TIMEOUT = 10.0

#: The session-factory seam for :func:`ask_condition` (defaults to a real ephemeral
#: :class:`~chief.core.backend.CopilotBackend` session; tests inject a fake).
CondSessionFactory = Callable[..., SessionProto]

_CONDITION_SYSTEM = (
    "You judge whether a condition currently holds. Use your read-only tools to check "
    "if you need to, then answer with only YES (it holds now) or NO (it does not)."
)

_STOP_SYSTEM = (
    "You classify a message a user sent while the assistant is mid-task. "
    "Answer YES only if it tells the assistant to STOP, abandon, or redirect the "
    "current work (e.g. 'stop', 'no wait', 'cancel that', 'actually do X instead'). "
    "Answer NO if it is extra info, a follow-up, or unrelated. Reply only YES or NO."
)

_WARRANTS_SYSTEM = (
    "You decide whether a casual message should become a tracked task with its own "
    "thread. Answer YES only if it is concrete work to do or follow up on (a request, "
    "a multi-step ask). Answer NO for greetings, small talk, quick questions, or "
    "acknowledgements. Reply with only YES or NO."
)


_COMPLEX_SYSTEM = (
    "You decide whether an owner's request needs the stronger, more expensive model. "
    "Answer YES only if it needs deep reasoning or multi-step planning (involved "
    "design, tricky debugging, careful analysis). Answer NO for simple, routine, or "
    "quick requests the default model handles well. Reply with only YES or NO."
)


def _first_message_content(data: Any) -> str:
    """Pull the assistant text from an OpenRouter chat-completions response, liberally.

    Shape: ``{"choices": [{"message": {"content": "..."}}]}``. Anything else (an error
    body, a reshaped payload) yields ``""`` so the caller fails safe.
    """
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else ""


async def _openrouter_chat(
    prompt: str,
    *,
    model: str,
    system: str,
    api_key: str | None,
    transport: httpx.AsyncBaseTransport | None,
    timeout: float,
) -> str:
    """One OpenRouter chat-completions turn; return the assistant text.

    Keyless → return ``""`` **without making any HTTP call** (the classifier then fails
    safe). ``transport`` is the httpx seam tests inject (``MockTransport``); production
    passes ``None`` for the real network. Mirrors the request shape of
    :class:`chief.tools.web.BraveSearcher`.
    """
    if not api_key:
        return ""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": _MAX_TOKENS,
        "temperature": 0,
    }
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        resp = await client.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    return _first_message_content(data)


async def ask_yes_no(
    prompt: str,
    *,
    model: str,
    system: str,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Run one OpenRouter one-shot; ``True`` iff the answer starts with 'yes'.

    Fails **safe** to ``False`` on any error, a non-2xx, or a missing key (in which case
    no HTTP call is made). These are pure yes/no judgments — no tool ever runs.
    """
    try:
        reply = await _openrouter_chat(
            prompt,
            model=model,
            system=system,
            api_key=api_key,
            transport=transport,
            timeout=_CLASSIFIER_TIMEOUT,
        )
    except Exception:
        logger.warning("classifier query failed; defaulting to NO", exc_info=True)
        return False
    return reply.strip().lower().startswith("yes")


async def stop_intent(
    text: str,
    *,
    model: str,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """True if a mid-turn ``text`` asks to stop/redirect the running turn."""
    return await ask_yes_no(
        text, model=model, system=_STOP_SYSTEM, api_key=api_key, transport=transport
    )


async def warrants_task(
    text: str,
    *,
    model: str,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """True if a casual ``text`` should spawn a tracked task topic."""
    return await ask_yes_no(
        text,
        model=model,
        system=_WARRANTS_SYSTEM,
        api_key=api_key,
        transport=transport,
    )


async def is_complex(
    text: str,
    *,
    model: str,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """True if an owner ``text`` needs the stronger model (deep reasoning/planning)."""
    return await ask_yes_no(
        text,
        model=model,
        system=_COMPLEX_SYSTEM,
        api_key=api_key,
        transport=transport,
    )


_CATEGORY_SYSTEM_TEMPLATE = (
    "You sort a user's request into exactly one job category. The categories are: "
    "{categories}. Reply with ONLY the single best-fitting category name, lowercase "
    "and nothing else. If unsure, answer 'general'."
)


def _category_label(category: str, descriptions: Mapping[str, str] | None) -> str:
    """``name`` or ``name (description)`` when the category has a description."""
    description = (descriptions or {}).get(category)
    return f"{category} ({description})" if description else category


async def classify_category(
    text: str,
    *,
    model: str,
    categories: Sequence[str],
    default: str,
    descriptions: Mapping[str, str] | None = None,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Sort ``text`` into one of ``categories`` on the fixed cheap ``model`` (#79).

    A direct OpenRouter one-shot (like :func:`ask_yes_no`) that returns a category name
    instead of a bool. Runs on the configured classifier model — **never** a routing
    target, so the classifier that feeds the routing table can't recurse through it.
    Fails **safe** to ``default``: any error, a non-2xx, a missing key (no HTTP call),
    an empty reply, or a reply naming no known category returns ``default``.

    The prompt — its label space *and* each category's optional ``descriptions`` blurb —
    is derived from the live category set, so a self-config edit (#83: an added,
    renamed, or re-described category) steers the classifier at the very next spawn.
    """
    if not categories:
        return default
    labels = [_category_label(c, descriptions) for c in categories]
    system = _CATEGORY_SYSTEM_TEMPLATE.format(categories=", ".join(labels))
    try:
        reply = await _openrouter_chat(
            text,
            model=model,
            system=system,
            api_key=api_key,
            transport=transport,
            timeout=_CLASSIFIER_TIMEOUT,
        )
    except Exception:
        logger.warning("category classifier failed; defaulting", exc_info=True)
        return default
    reply = reply.strip().lower()
    for category in categories:
        if category.lower() in reply:
            return category
    return default


def _read_only_gate(allowed: frozenset[str]) -> CanUseTool:
    """A ``can_use_tool`` closure that ALLOWs exactly ``allowed`` and DENYs the rest.

    This is the ephemeral monitor session's real gate (#88): a monitor may only touch
    the read tools it was handed, so anything off the allow-list is denied outright —
    the monitor can never write, spend, or reach a tool it wasn't scoped to.
    """

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        if tool_name in allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=f"{tool_name} is not available to this monitor"
        )

    return can_use_tool


async def ask_condition(
    question: str,
    *,
    model: str,
    allowed_tools: list[str],
    mcp_servers: dict[str, Any] | None = None,
    session_factory: CondSessionFactory | None = None,
    timeout: float = _CONDITION_TIMEOUT,
) -> bool:
    """True iff an agent judges ``question`` holds now — read-only, fail-safe to False.

    Runs one turn of an **ephemeral Copilot session** (the tool-using judgment can't be
    a cheap HTTP one-shot: it must be able to call a read tool then answer). The session
    is scoped to ``allowed_tools`` — the read surface (``mcp_servers`` are the owner's
    Google HTTP servers) — with a ``can_use_tool`` gate (:func:`_read_only_gate`) that
    allow-lists exactly those tools and denies everything else. The answer is the last
    non-empty final text, ``YES``/``NO``; anything else, or any exception, returns
    ``False`` (a monitor never flips wrong). The whole call is awaited inline in the
    scheduler tick loop, so both phases are time-boxed: the turn by ``timeout`` seconds
    (default :data:`_CONDITION_TIMEOUT`, 120s) and the teardown that follows it by
    :data:`_CLOSE_TIMEOUT` (10s) — a wedged turn times out to ``False`` and its
    now-wedged session close is bounded too, so the worst-case inline block is their
    sum, never unbounded. ``session_factory`` defaults to a real
    :class:`~chief.core.backend.CopilotBackend` session; tests inject a fake.
    """
    factory = (
        session_factory
        if session_factory is not None
        else CopilotBackend().create_session
    )
    session = factory(
        model=model,
        system_prompt=_CONDITION_SYSTEM,
        can_use_tool=_read_only_gate(frozenset(allowed_tools)),
        allowed_tools=list(allowed_tools),
        mcp_servers=mcp_servers or {},
    )
    answer = ""
    try:
        # TimeoutError is an Exception, so a wedged turn is caught here → False, and the
        # finally below still tears the session down.
        async with asyncio.timeout(timeout):
            async for event in session.run_turn(question):
                if isinstance(event, Final):
                    stripped = event.text.strip()
                    if stripped:  # keep the latest non-empty text as the answer
                        answer = stripped
    except Exception:
        logger.warning("condition query failed; defaulting to NO", exc_info=True)
        return False
    finally:
        try:
            # On the timeout path the session is wedged by definition, and aclose's
            # disconnect/stop is itself unbounded — time-box it (mirrors tasks.py's
            # _RESET_TIMEOUT) so a hung teardown can't re-freeze the tick loop. A
            # TimeoutError here is an Exception, so it is swallowed by this except.
            async with asyncio.timeout(_CLOSE_TIMEOUT):
                await session.aclose()
        except Exception:
            logger.debug("ask_condition session close failed", exc_info=True)
    return answer.lower().startswith("yes")
