"""The strings the gate emits: the announce line to the owner, the denial
back to the model.

Split from ``gate.py`` (which decides and enforces) so wording changes don't
push that file past the 200-line cap.

The old denial ("denied by the gate") said only that something refused, which
reliably produced the wrong next move — denied ``write_file``, the model
reaches for a ``shell`` heredoc and gets the same effect anyway. So both texts
name the workarounds and forbid them.

They are two texts rather than one because the right next move differs. A card
decline is a decision the owner can revisit, so it may be retried once they
say so; a never-list entry can only be changed by the owner editing config, so
the model must not go looking for another route — including editing that
config itself, which the self-edit skill otherwise teaches it to do.
"""

import json

from chief.provider.base import ToolCall

# Arguments are rendered into the announcement line; a shell heredoc or a
# whole file body would otherwise flood the owner's channel.
ANNOUNCE_ARG_LIMIT = 160


def announce_text(call: ToolCall, *, denied: bool = False) -> str:
    """The one-line "running this now" notice for a non-card tool call."""
    arguments = json.dumps(call.arguments, default=str)
    if len(arguments) > ANNOUNCE_ARG_LIMIT:
        arguments = arguments[:ANNOUNCE_ARG_LIMIT] + "…"
    suffix = " — denied by the gate" if denied else ""
    return f"⚙ {call.name} {arguments}{suffix}"


def card_denied(tool_name: str) -> str:
    """The owner's approval card came back a no.

    ``Approval.DENY`` also covers the 600s timeout, an unparseable answer, and
    a card refused because one was already pending — so this must not claim
    the owner typed "no", only that no approval arrived.
    """
    return (
        f"error: the owner did not approve tool {tool_name!r} — they declined, "
        "or the card went unanswered. Treat this as a no. Do not retry it on "
        "your own and do not reach the same effect another way (a shell "
        "equivalent, a different tool). Stop and tell the owner what you were "
        "about to do and why; if they then tell you to go ahead, you may."
    )


def never_denied(tool_name: str) -> str:
    """The tool is on the gate's never list."""
    return (
        f"error: tool {tool_name!r} is on the gate's never list. No approval "
        "card can lift that — only the owner can, by changing `gate.never` in "
        "config.yaml. Do not retry it, do not edit that config yourself to "
        "lift it, and do not reach the same effect another way (a shell "
        "equivalent, a different tool). Stop this line of work and tell the "
        "owner what you needed it for."
    )
