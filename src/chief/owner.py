"""Where chief reaches the owner when nobody has just spoken to it."""

from chief.config import Config


def primary_target(config: Config) -> tuple[str, str]:
    """The owner's primary channel: the iMessage self-chat, else the web UI."""
    if config.imessage_enabled and config.imessage_owner_handles:
        return "imessage", config.imessage_owner_handles[0]
    return "web", "web:main"
