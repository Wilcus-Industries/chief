"""Native tool: the agent switches THIS thread's model, approval-gated.

Gray by construction — ``read_only`` is left False, so the gate raises an
approval card before a thread can hop onto another model (e.g. a metered
subscription backend). No self-thread guard is needed: ``set_model`` only
records an override and does not race the in-flight transcript commit.
"""

from chief.agent.manager import SessionManager
from chief.provider.base import ToolSpec
from chief.provider.model_names import UnknownModelError, validate_model_name
from chief.tools import Tool, ToolContext, ToolRegistry

_SPEC = ToolSpec(
    name="switch_model",
    description=(
        "Switch the model THIS thread runs on, by name. Pass `model` as a "
        "configured alias (e.g. 'opus') or a backend model id; a name with no "
        "alias falls through to the default backend. Takes effect on the NEXT "
        "turn, not the current one, and may require owner approval."
    ),
    parameters={
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "description": "alias or model id to run this thread on",
            },
        },
        "required": ["model"],
    },
)


def register_switch_model_tool(
    registry: ToolRegistry,
    manager: SessionManager,
    model_aliases: frozenset[str] = frozenset(),
) -> None:
    """Expose the approval-gated ``switch_model`` tool backed by the manager."""

    async def switch_model(model: str, context: ToolContext | None = None) -> str:
        if context is None:
            return "error: switch_model needs a session context"
        try:
            name = validate_model_name(
                model, aliases=model_aliases, default_model=manager.default_model
            )
        except UnknownModelError as exc:
            return f"error: {exc}"
        await manager.set_model(context.thread_key, context.channel, name)
        return f"model set to '{name}' for this thread; takes effect next turn"

    registry.register(Tool(_SPEC, switch_model, wants_context=True))
