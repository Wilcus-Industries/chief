"""The strings the gate emits: the announce line to the owner, the denial
back to the model.

Split from ``gate.py`` (which decides and enforces) so wording changes don't
push that file past the 200-line cap. The two denial texts are one decision:
a denial has to tell the model *which* no it hit, since the right next move
differs.

The old single line ("denied by the gate") said only that something refused,
which reliably produced the wrong next move — denied ``write_file``, the
model reaches for a ``shell`` heredoc and gets the same effect anyway. Saying
**stop** is the entire point of these strings.
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
    a card refused because one was already pending on the thread — so this
    must not claim the owner typed "no", only that no approval arrived. It is
    still a decision the owner can revisit, so the model is told to surface it
    rather than to give up on the goal.
    """
    return (
        f"error: the owner did not approve tool {tool_name!r} — they declined, "
        "or the card went unanswered. Treat this as a no. Do not retry the "
        "call, do not re-ask, and do not reach the same effect another way (a "
        "shell equivalent, a different tool). Stop what you were doing and "
        "tell the owner what you were about to do and why, so they can decide."
    )


def never_denied(tool_name: str) -> str:
    """The tool is on the gate's never list.

    Kept distinct from the card text: no owner was asked and no approval can
    lift it, so "the owner declined" would invite a re-ask that can never
    succeed.
    """
    return (
        f"error: tool {tool_name!r} is on the gate's never list — permanently "
        "denied, and no approval can lift it. Do not retry it and do not reach "
        "the same effect another way (a shell equivalent, a different tool). "
        "Stop this line of work and tell the owner what you needed it for."
    )
