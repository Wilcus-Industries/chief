"""Chief-owned in-process tool seam (#88).

chief's shell, scheduler, guest, web, routing-admin, and Google-account tools all run
**in-process** — the tool logic executes inside the core process, not a subprocess. That
seam used to be spelled in claude-agent-sdk's ``@tool`` decorator +
``create_sdk_mcp_server`` (which built a live ``mcp.Server`` whose handlers chief then
drove). With claude-agent-sdk removed, chief owns the seam here — a plain
``(name, description, input_schema, handler)`` record and a config that just carries the
list of them. No ``mcp.Server`` round-trip, and no promoting ``mcp`` (a claude-agent-sdk
transitive) to a direct dependency.

:func:`chief.core.copilot_tools.partition_mcp_servers` reads an
:class:`InProcessServerConfig` (``type == "sdk"``) and converts each
:class:`InProcessTool` straight into a :class:`copilot.Tool`, calling
:func:`build_input_schema` for the JSON schema. Keeping the same ``@tool`` /
``create_sdk_mcp_server`` names and the same handler contract
(``async (args: dict) -> {"content": [...], "is_error": bool}``) means the tool
modules and their tests carry over with only an import swap.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

#: A tool handler: ``async (args) -> {"content": [...], "is_error": bool}``.
ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

#: Python types chief's simple ``{param: type}`` schemas use, mapped to JSON Schema.
_JSON_TYPES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


@dataclass(frozen=True)
class InProcessTool:
    """One in-process tool: its name, description, input schema, and handler.

    ``input_schema`` is either a simple ``{param_name: python_type}`` mapping or a full
    JSON Schema dict (``{"type": "object", "properties": …}``);
    :func:`build_input_schema` normalises both.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


class InProcessServerConfig(TypedDict):
    """An in-process tool server, mixed into a session's ``mcp_servers`` mapping.

    ``type == "sdk"`` is the discriminator :func:`partition_mcp_servers` keys off to
    tell an in-process server from an HTTP MCP server; ``name`` is the server name the
    tools qualify under (``mcp__<name>__<tool>``).
    """

    type: Literal["sdk"]
    name: str
    tools: list[InProcessTool]


def tool(
    name: str, description: str, input_schema: dict[str, Any]
) -> Callable[[ToolHandler], InProcessTool]:
    """Decorator building an :class:`InProcessTool` from an async handler.

    Drop-in for claude-agent-sdk's ``@tool`` (the same three positional args and the
    same handler contract), so the tool modules keep their existing definitions.
    """

    def decorator(handler: ToolHandler) -> InProcessTool:
        return InProcessTool(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
        )

    return decorator


def create_sdk_mcp_server(
    name: str, version: str = "1.0.0", tools: list[InProcessTool] | None = None
) -> InProcessServerConfig:
    """Bundle in-process tools into a server config (drop-in for the SDK's).

    ``version`` is accepted for call-site compatibility and ignored — chief's in-process
    seam has no wire version.
    """
    return InProcessServerConfig(type="sdk", name=name, tools=tools or [])


def build_input_schema(input_schema: dict[str, Any]) -> dict[str, Any]:
    """Normalise a tool's ``input_schema`` to a JSON Schema object.

    Two shapes, matching what claude-agent-sdk's server builder accepted:

    - A full JSON Schema object (has a string ``"type"`` and ``"properties"``) →
      returned as-is.
    - A simple ``{param_name: python_type}`` mapping → an object schema whose properties
      are the mapped JSON types, with **every** parameter required (chief's tools use no
      optional params in this form; the full-schema form carries its own ``required``).
    """
    if (
        "type" in input_schema
        and "properties" in input_schema
        and isinstance(input_schema["type"], str)
    ):
        return input_schema
    properties = {
        param: {"type": _JSON_TYPES.get(py_type, "string")}
        for param, py_type in input_schema.items()
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
    }
