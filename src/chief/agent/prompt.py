"""System prompt loading.

The prompt lives in an editable file (a self-edit target for the agent); the
built-in default only covers a box where that file doesn't exist yet.
"""

from pathlib import Path

DEFAULT_SYSTEM_PROMPT = (
    "You are chief, a personal agent running as a daemon on your owner's "
    "always-on machine. Be direct and concise. Use your tools when a task "
    "needs them; answer plainly when it doesn't."
)

SYSTEM_PROMPT_PATH = Path("data/system.md")


def system_prompt(path: Path = SYSTEM_PROMPT_PATH) -> str:
    """The current system prompt: the editable file if present, else default."""
    if path.exists():
        text = path.read_text().strip()
        if text:
            return text
    return DEFAULT_SYSTEM_PROMPT
