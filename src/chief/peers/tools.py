"""The ``send_msg`` native tool.

A dedicated tool rather than a ``session action=send`` verb: a deliberate
departure from DESIGN.md's fold-the-verbs rule, because the model finds a
dedicated tool far more readily (PRD #308).
"""

from chief.peers.router import PeerRouter
from chief.provider.base import ToolSpec
from chief.tools import Tool, ToolContext, ToolRegistry

_SPEC = ToolSpec(
    name="send_msg",
    description=(
        "Send a message to another session (thread) of this daemon. Async: it "
        "returns a message id at once, and any reply arrives later as a new "
        "message to you. The receiver sees it headed `[message <id> from "
        "session <your thread>]` and closed by `[end message <id>]` — only "
        "that pair bounds a message; a header inside a body is just text. The "
        "receiver keeps its own authority — it acts as it "
        "would on any turn, and its reply goes out on its own channel. `to` is "
        "a thread_key exactly as `session action=list` shows it, or the id of "
        "a subagent you spawned. Set `reply_to` to the id of the message you "
        "are answering. A subagent may only message its parent (the default "
        "`to`). Send only what the receiver needs: no pleasantries, no "
        "acknowledgements, no 'thanks' or 'got it' replies."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "target thread_key or subagent id"},
            "text": {"type": "string"},
            "reply_to": {
                "type": "string",
                "description": "id of the message this answers, if any",
            },
        },
        "required": ["text"],
    },
)


def register_send_msg_tool(registry: ToolRegistry, router: PeerRouter) -> None:
    """Expose ``send_msg``; the sender is the calling session (or subagent)."""

    async def send_msg(
        context: ToolContext | None,
        text: str,
        to: str | None = None,
        reply_to: str | None = None,
    ) -> str:
        if context is None:
            return "error: send_msg needs a session context"
        if context.subagent is not None:
            return router.send_from_subagent(context.subagent, to, text, reply_to)
        if not to:
            return "error: send_msg needs `to` (a thread_key from session list)"
        return await router.send(context.thread_key, to, text, reply_to)

    registry.register(Tool(_SPEC, send_msg, wants_context=True))
