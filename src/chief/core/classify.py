"""Cheap Haiku judgments used by the task engine.

Three yes/no classifiers, each a single ``max_turns=1`` query on the configured
classifier model (Haiku): ``stop_intent`` (does a mid-turn message mean "stop / do X
instead"?), ``warrants_task`` (should a casual message become a tracked task topic?),
and ``is_complex`` (does an owner task need the stronger, pricier model?). All fail
**safe** — any error or ambiguity returns ``False`` (no interrupt, no spawn, no
escalation).

:func:`ask_condition` is a further, heavier judgment used by the scheduler's agent
monitors: the same yes/no shape but **with read-only tools** (web + the owner's Google
reads) so Haiku can fetch/look something up before answering. It too fails safe to
``False`` — a monitor must never flip on an evaluation error.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

logger = logging.getLogger("chief.core.classify")

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


async def ask_yes_no(prompt: str, *, model: str, system: str) -> bool:
    """Run one Haiku turn and return ``True`` iff the answer starts with 'yes'.

    ``tools=[]`` gives the model an empty base tool set so it cannot call anything —
    a single yes/no turn is all it can do. ``allowed_tools=[]`` is NOT enough: that
    field is only an auto-approve allowlist, and the SDK skips the ``--allowedTools``
    flag entirely when it is empty (falsy), so the CLI falls back to its *default*
    tool set. The model then spends the one turn on a tool call and dies with
    ``Reached maximum number of turns (1)`` before answering. These are pure yes/no
    judgments — no tool ever needs to run.
    """
    options = ClaudeAgentOptions(
        max_turns=1, model=model, system_prompt=system, tools=[], allowed_tools=[]
    )
    parts: list[str] = []
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                parts += [b.text for b in message.content if isinstance(b, TextBlock)]
    except Exception:
        logger.warning("classifier query failed; defaulting to NO", exc_info=True)
        return False
    return "".join(parts).strip().lower().startswith("yes")


async def stop_intent(text: str, *, model: str) -> bool:
    """True if a mid-turn ``text`` asks to stop/redirect the running turn."""
    return await ask_yes_no(text, model=model, system=_STOP_SYSTEM)


async def warrants_task(text: str, *, model: str) -> bool:
    """True if a casual ``text`` should spawn a tracked task topic."""
    return await ask_yes_no(text, model=model, system=_WARRANTS_SYSTEM)


async def is_complex(text: str, *, model: str) -> bool:
    """True if an owner ``text`` needs the stronger model (deep reasoning/planning)."""
    return await ask_yes_no(text, model=model, system=_COMPLEX_SYSTEM)


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
) -> str:
    """Sort ``text`` into one of ``categories`` on the fixed cheap ``model`` (#79).

    Reuses the :func:`ask_yes_no` harness (a single ``max_turns=1`` turn with an empty
    base tool set, so the model can only answer) but returns a category name instead of
    a bool. Runs on the configured classifier model — **never** on a routing target, so
    the classifier that feeds the routing table can't recurse through it. Fails **safe**
    to ``default``: any error, an empty reply, or a reply naming no known category
    returns ``default`` (the general fallback), so a mis-parse never wedges a spawn.

    The prompt — its label space *and* each category's optional ``descriptions`` blurb —
    is derived from the live category set, so a self-config edit (#83: an added,
    renamed, or re-described category) steers the classifier at the very next spawn.
    """
    if not categories:
        return default
    labels = [_category_label(c, descriptions) for c in categories]
    system = _CATEGORY_SYSTEM_TEMPLATE.format(categories=", ".join(labels))
    options = ClaudeAgentOptions(
        max_turns=1, model=model, system_prompt=system, tools=[], allowed_tools=[]
    )
    parts: list[str] = []
    try:
        async for message in query(prompt=text, options=options):
            if isinstance(message, AssistantMessage):
                parts += [b.text for b in message.content if isinstance(b, TextBlock)]
    except Exception:
        logger.warning("category classifier failed; defaulting", exc_info=True)
        return default
    reply = "".join(parts).strip().lower()
    for category in categories:
        if category.lower() in reply:
            return category
    return default


async def ask_condition(
    question: str,
    *,
    model: str,
    allowed_tools: list[str],
    mcp_servers: dict[str, Any] | None = None,
    max_turns: int = 4,
) -> bool:
    """True iff Haiku judges ``question`` holds now — read-only, fail-safe to ``False``.

    Runs a short multi-turn query (``max_turns`` so it can call a read tool then answer)
    on the classifier ``model`` with ``allowed_tools`` / ``mcp_servers`` as its only
    surface. The answer is the final non-empty text block, ``YES``/``NO``; anything
    else, a tool error, or an exception returns ``False`` (a monitor never flips wrong).
    """
    options = ClaudeAgentOptions(
        max_turns=max_turns,
        model=model,
        system_prompt=_CONDITION_SYSTEM,
        allowed_tools=allowed_tools,
        mcp_servers=mcp_servers or {},
    )
    answer = ""
    try:
        async for message in query(prompt=question, options=options):
            if isinstance(message, AssistantMessage):
                texts = [b.text for b in message.content if isinstance(b, TextBlock)]
                joined = "".join(texts).strip()
                if joined:  # keep the latest non-empty text as the standing answer
                    answer = joined
    except Exception:
        logger.warning("condition query failed; defaulting to NO", exc_info=True)
        return False
    return answer.lower().startswith("yes")
