"""Cheap Haiku judgments used by the task engine.

Two yes/no classifiers, each a single ``max_turns=1`` query on the configured
classifier model (Haiku): ``stop_intent`` (does a mid-turn message mean "stop / do X
instead"?) and ``warrants_task`` (should a casual message become a tracked task topic?).
Both fail **safe** — any error or ambiguity returns ``False`` (no interrupt, no spawn).
"""

import logging

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

logger = logging.getLogger("chief.core.classify")

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
    """Run one Haiku turn and return ``True`` iff the answer starts with 'yes'."""
    options = ClaudeAgentOptions(max_turns=1, model=model, system_prompt=system)
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
