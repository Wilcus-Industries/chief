"""The peer message envelope and how a receiver sees it.

A plain envelope, not an A2A Task (PRD #308): ``{id, from, to, reply_to?,
text}``, text only. It would sit inside a Task unchanged if a network
transport ever arrives. The receiver reads the header to learn who sent it and
which id to answer with ``reply_to``. Keep the header shape stable: core skills
(e.g. relaying a forwarded note back to its sender) key on it.
"""

import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class PeerMessage:
    """One message between two sessions (or a subagent and its parent)."""

    id: str
    sender: str
    to: str
    text: str
    reply_to: str | None = None


def new_id() -> str:
    """A fresh message id, ``m-<8 hex>``."""
    return f"m-{secrets.token_hex(4)}"


def session_label(thread_key: str) -> str:
    """How a session sender is named in the header."""
    return f"session {thread_key}"


def subagent_label(run_id: str, agent_name: str) -> str:
    """How a subagent sender is named in the header."""
    return f"subagent {run_id} ({agent_name})"


def render(message: PeerMessage, label: str, warning: str | None = None) -> str:
    """The text the receiver gets as its turn input or injected message:
    ``[message m-… from <label> re m-…]`` then the body, then any loop warning."""
    regarding = f" re {message.reply_to}" if message.reply_to else ""
    text = f"[message {message.id} from {label}{regarding}]\n{message.text}"
    return f"{text}\n{warning}" if warning else text
