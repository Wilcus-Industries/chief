"""Cheap Haiku judgments used by the task engine.

Two yes/no classifiers, each a single ``max_turns=1`` query on the configured
classifier model (Haiku): ``stop_intent`` (does a mid-turn message mean "stop / do X
instead"?) and ``warrants_task`` (should a casual message become a tracked task topic?).
Both fail **safe** — any error or ambiguity returns ``False`` (no interrupt, no spawn).

:func:`ask_condition` is a third, heavier judgment used by the scheduler's agent
monitors: the same yes/no shape but **with read-only tools** (web + the owner's Google
reads) so Haiku can fetch/look something up before answering. It too fails safe to
``False`` — a monitor must never flip on an evaluation error.
"""

import logging
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


async def _ask_yes_no(prompt: str, *, model: str, system: str) -> bool:
    """Run one Haiku turn and return ``True`` iff the answer starts with 'yes'.

    ``allowed_tools=[]`` keeps the bundled CLI from auto-calling a default tool, which
    would spend the single turn on the tool call and die with ``Reached maximum number
    of turns (1)`` before answering (fail-safe NO, but noisy). These are pure yes/no
    judgments — no tool ever needs to run.
    """
    options = ClaudeAgentOptions(
        max_turns=1, model=model, system_prompt=system, allowed_tools=[]
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
    return await _ask_yes_no(text, model=model, system=_STOP_SYSTEM)


async def warrants_task(text: str, *, model: str) -> bool:
    """True if a casual ``text`` should spawn a tracked task topic."""
    return await _ask_yes_no(text, model=model, system=_WARRANTS_SYSTEM)


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
