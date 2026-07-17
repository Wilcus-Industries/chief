"""System prompt loading.

The prompt lives in an editable file (a self-edit target for the agent); the
built-in default only covers a box where that file doesn't exist yet. If the
soul package is installed, its ``Soul.md`` is inlined at the very top so the
agent's character is always in context — read fresh every build, so evolving
the soul takes effect next turn with no per-session tool read.
"""

from pathlib import Path

DEFAULT_SYSTEM_PROMPT = (
    "You are chief, a personal agent running as a daemon on your owner's "
    "always-on machine. Be direct and concise. Use your tools when a task "
    "needs them; answer plainly when it doesn't."
)

SYSTEM_PROMPT_PATH = Path("data/system.md")

# The soul package seeds this; absent until it is installed. Inlined verbatim
# at the top of the prompt so character leads and tracks its own edits.
SOUL_PATH = Path("data/memory/Soul.md")

# Appended on a fresh install: the first conversation *is* onboarding.
ONBOARDING_SUFFIX = (
    "\n\nThis is a fresh install and this first conversation is onboarding: "
    "introduce yourself briefly, mention the owner can create monitors and "
    "recurring schedules by asking, and offer to help set up additional "
    "channels or packages."
)


def system_prompt(
    path: Path = SYSTEM_PROMPT_PATH, soul_path: Path = SOUL_PATH
) -> str:
    """The current system prompt: the editable file if present, else default,
    with ``Soul.md`` inlined at the top when the soul package is installed."""
    base = DEFAULT_SYSTEM_PROMPT
    if path.exists():
        text = path.read_text().strip()
        if text:
            base = text
    if soul_path.exists() and (soul := soul_path.read_text().strip()):
        return f"{soul}\n\n{base}"
    return base
