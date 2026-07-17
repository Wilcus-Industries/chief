"""System prompt loading.

Two pieces with different lifecycles:

- The **base prompt** lives in an editable file (a self-edit target for the
  agent); the built-in default only covers a box where that file doesn't exist
  yet. It is boot-static — changing it goes through the self-edit pipeline,
  which restarts the daemon.
- The **soul** (``data/memory/Soul.md``, seeded by the soul package) is the
  agent's character. It is edited with the ordinary file/git tools, *not* the
  self-edit pipeline, so it must be read fresh — ``read_soul`` is called by the
  session per turn and inlined at the top of the prompt, so evolving the soul
  takes effect on the next turn without a restart.
"""

from pathlib import Path

DEFAULT_SYSTEM_PROMPT = (
    "You are chief, a personal agent running as a daemon on your owner's "
    "always-on machine. Be direct and concise. Use your tools when a task "
    "needs them; answer plainly when it doesn't."
)

SYSTEM_PROMPT_PATH = Path("data/system.md")

# The soul package seeds this; absent until it is installed.
SOUL_PATH = Path("data/memory/Soul.md")

# Appended on a fresh install: the first conversation *is* onboarding.
ONBOARDING_SUFFIX = (
    "\n\nThis is a fresh install and this first conversation is onboarding: "
    "introduce yourself briefly, mention the owner can create monitors and "
    "recurring schedules by asking, and offer to help set up additional "
    "channels or packages."
)


def system_prompt(path: Path = SYSTEM_PROMPT_PATH) -> str:
    """The boot-static base prompt: the editable file if present, else default."""
    if path.exists():
        text = path.read_text().strip()
        if text:
            return text
    return DEFAULT_SYSTEM_PROMPT


def read_soul(soul_path: Path = SOUL_PATH) -> str:
    """The owner's ``Soul.md`` character text, read fresh (empty when the soul
    package isn't installed). Read per turn by the session and inlined at the
    top of the prompt, so soul edits take effect next turn with no restart.

    Never raises: an absent, unreadable, or non-UTF-8 file is a no-op (empty
    string), so a hand-edited soul can never crash the daemon.
    """
    try:
        return soul_path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""
