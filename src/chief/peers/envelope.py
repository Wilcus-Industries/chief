"""The peer message envelope and how a receiver sees it.

A plain envelope, not an A2A Task (PRD #308): ``{id, from, to, reply_to?,
text}``, text only. It would sit inside a Task unchanged if a network
transport ever arrives. The receiver reads the header to learn who sent it and
which id to answer with ``reply_to``. Keep the header shape stable: core skills
(e.g. relaying a forwarded note back to its sender) key on it.
"""

import secrets
from dataclasses import dataclass

from chief.policy import frame_untrusted


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


def render(
    message: PeerMessage, label: str, warning: str | None = None,
    untrusted: bool = False,
) -> str:
    """The text the receiver gets as its turn input or injected message:
    ``[message m-… from <label> re m-…]``, the body, ``[end message m-…]``,
    then any loop warning (code's, so outside the frame).

    The close marker carries the id, minted after the sender wrote its text,
    so a body can't fake its end and forge a second header. ``untrusted``
    (a sender whose thread reaches a non-owner, so may carry injected text)
    also wraps the body in the nonce frame a monitor wake gets."""
    regarding = f" re {message.reply_to}" if message.reply_to else ""
    body = frame_untrusted(message.text) if untrusted else message.text
    text = (
        f"[message {message.id} from {label}{regarding}]\n{body}\n"
        f"[end message {message.id}]"
    )
    return f"{text}\n{warning}" if warning else text
