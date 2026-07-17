"""Native tool for the agent to manage its own sessions (threads)."""

from chief.agent.manager import SessionManager
from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.provider.base import ToolSpec

_SPEC = ToolSpec(
    name="session",
    description=(
        "Manage the threads (sessions) this daemon knows about. "
        "action=list takes nothing and reports every thread. "
        "action=create needs `thread_key` (plus optional `channel`, defaulting "
        "to this thread's channel); it pre-registers the thread row so it shows "
        "up in list and the web cockpit, without starting a live session. "
        "action=delete needs `thread_key` and removes the thread entirely. "
        "action=clear needs `thread_key` and wipes its transcript, keeping the "
        "row. delete/clear refuse your own thread and any thread mid-turn."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create", "delete", "clear"],
            },
            "thread_key": {"type": "string"},
            "channel": {
                "type": "string",
                "description": "channel for create; defaults to this thread's",
            },
        },
        "required": ["action"],
    },
)


def _busy_error(thread_key: str) -> str:
    """Refusal string when a delete/clear targets a thread mid-turn."""
    return f"error: session '{thread_key}' is mid-turn; try again once idle"


def register_session_tools(registry: ToolRegistry, manager: SessionManager) -> None:
    """Expose the session tool (list/create/delete/clear) backed by the manager."""

    async def _list() -> str:
        rows = await manager.list_sessions()
        if not rows:
            return "no sessions"
        return "\n".join(
            f"[{r['channel']}] {r['thread']} — {r['count']} msgs, last {r['last']}"
            for r in rows
        )

    async def _create(
        context: ToolContext, thread_key: str, channel: str | None
    ) -> str:
        await manager.create(thread_key, channel or context.channel)
        return f"session '{thread_key}' registered"

    async def _delete(context: ToolContext, thread_key: str) -> str:
        if thread_key == context.thread_key:
            return (
                "error: the thread you are mid-turn in can't self-delete — it "
                "races the in-flight transcript commit; the owner must remove "
                "it (or clear it with the /clear command)."
            )
        if not await manager.delete(thread_key):
            return _busy_error(thread_key)
        return f"session '{thread_key}' deleted"

    async def _clear(context: ToolContext, thread_key: str) -> str:
        if thread_key == context.thread_key:
            return (
                "error: refusing to clear the thread you are mid-turn in — it "
                "races the in-flight transcript commit. Use the /clear owner "
                "command, which runs before the turn."
            )
        if not await manager.clear(thread_key):
            return _busy_error(thread_key)
        return f"session '{thread_key}' cleared"

    async def session(
        action: str,
        context: ToolContext | None = None,
        thread_key: str | None = None,
        channel: str | None = None,
    ) -> str:
        if context is None:
            return "error: session needs a session context"
        if action == "list":
            return await _list()
        if action not in ("create", "delete", "clear"):
            return f"error: unknown action '{action}'"
        if not thread_key:
            return f"error: {action} needs thread_key"
        if action == "create":
            return await _create(context, thread_key, channel)
        if action == "delete":
            return await _delete(context, thread_key)
        return await _clear(context, thread_key)

    registry.register(Tool(_SPEC, session, wants_context=True))
