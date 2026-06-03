"""Agent SDK wrapper — a single owner turn (no tools yet).

M1 scope: a stateless one-shot owner chat that runs on the Max subscription. The task
engine (persistent streaming sessions, steering, resume) lands at M2. This supersedes
the throwaway ``s0/bot.py:ask_claude``.
"""

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

NO_REPLY = "(no reply)"


async def owner_oneshot(prompt: str, *, model: str) -> str:
    """Run one ``max_turns=1`` SDK query and return the assistant's text.

    Args:
        prompt: the owner's message text.
        model: model id to run (e.g. the configured owner default).

    Returns:
        The concatenated assistant text, or ``NO_REPLY`` if the turn produced none.
    """
    options = ClaudeAgentOptions(max_turns=1, model=model)
    parts: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            parts += [b.text for b in message.content if isinstance(b, TextBlock)]
    return "".join(parts).strip() or NO_REPLY
