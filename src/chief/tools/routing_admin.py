"""chief's self-config routing tool: amend the routing table at runtime (#83, part #72).

An owner-only in-process MCP server (``chief_routing``) that lets chief edit its **own**
model-routing config on request — both columns of a category's target
(``{target_class, model}``) and the **category set itself** (add, remove, rename,
re-describe). Every edit writes through :class:`~chief.core.routing.RoutingStore` to the
persisted ``routes`` table and reloads the in-memory cache, so the change survives a
restart and drives the very next classify/route (the classifier prompt is derived from
the live category set + descriptions).

Built the :mod:`chief.tools.shell` / :mod:`chief.tools.web` way (``@tool`` +
:func:`create_sdk_mcp_server`): one in-process MCP server reaches **both** backends via
the #80 :func:`chief.core.copilot_tools.partition_mcp_servers` adapter, so the tool is
written once, not per backend.

**This is a self-modification tool — the gate is the security boundary.** Under chief's
owner default-allow posture a merely-registered tool would ALLOW with no card, so every
*mutating* tool name is seeded into ``blacklist_tools`` (:mod:`chief.config`): a
blacklist match means ASK (never DENY), so each edit raises an approval card. The
read-only ``list_routing`` is left off the blacklist — it ALLOWs freely.

**Guardrails stay unreachable.** Guest isolation (#82) and budget caps (#84) key off a
target's **class**. This tool only edits the ``routes`` table (a category → class /
model / description) and has no knob for guest-class allowlists or budget config, so it
cannot grant a guest an ``openrouter`` target or raise/remove a cap. It is
wired only into *owner* sessions (never guests — tier isolation by construction), and
the store refuses any ``target_class`` outside
:data:`~chief.core.routing.TARGET_CLASSES`, so it can never invent a class the
downstream guardrails don't already cover.
"""

from dataclasses import dataclass
from typing import Any

from ..core.routing import RoutingEditError, RoutingStore
from .inprocess import (
    InProcessServerConfig,
    InProcessTool,
    create_sdk_mcp_server,
    tool,
)

#: The SDK names an in-process MCP tool ``mcp__<server>__<tool>`` — the exact strings
#: chief's gate + blacklist key off (see :mod:`chief.config`).
SERVER_NAME = "chief_routing"


def _tool_name(bare: str) -> str:
    """The SDK-qualified ``mcp__chief_routing__<bare>`` name the gate keys off."""
    return f"mcp__{SERVER_NAME}__{bare}"


SET_TARGET_TOOL = _tool_name("set_target")
ADD_CATEGORY_TOOL = _tool_name("add_category")
REMOVE_CATEGORY_TOOL = _tool_name("remove_category")
RENAME_CATEGORY_TOOL = _tool_name("rename_category")
DESCRIBE_CATEGORY_TOOL = _tool_name("describe_category")
LIST_ROUTING_TOOL = _tool_name("list_routing")

#: The mutating tool names, seeded into ``blacklist_tools`` (:mod:`chief.config`) so
#: each edit raises an approval card under the owner default-allow gate. The read-only
#: ``list_routing`` is deliberately excluded — it ALLOWs with no card.
MUTATING_TOOL_NAMES: tuple[str, ...] = (
    SET_TARGET_TOOL,
    ADD_CATEGORY_TOOL,
    REMOVE_CATEGORY_TOOL,
    RENAME_CATEGORY_TOOL,
    DESCRIBE_CATEGORY_TOOL,
)


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """An MCP tool result carrying one text block (mirrors chief's other tools)."""
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


_SET_TARGET_DESCRIPTION = (
    "Change which model/target-class an existing routing category maps to. "
    "target_class is 'copilot' (Copilot quota, e.g. model 'auto') or 'openrouter' (a "
    "BYOK per-model target). Takes effect at the next task spawn and survives restart. "
    "Requires the owner's approval."
)
_ADD_CATEGORY_DESCRIPTION = (
    "Add a new job category to the routing table with its target (target_class + "
    "model) and an optional description the classifier uses to sort requests into it. "
    "The classifier picks up the new category at the next spawn. Requires approval."
)
_REMOVE_CATEGORY_DESCRIPTION = (
    "Remove a job category from the routing table. The default fallback category "
    "cannot be removed. The classifier drops it at the next spawn. Requires approval."
)
_RENAME_CATEGORY_DESCRIPTION = (
    "Rename a job category (keeping its target and description). The default fallback "
    "category cannot be renamed. Requires approval."
)
_DESCRIBE_CATEGORY_DESCRIPTION = (
    "Set or replace a category's description — the blurb the classifier uses to decide "
    "what belongs in it. Pass an empty description to clear it. Requires approval."
)
_LIST_ROUTING_DESCRIPTION = (
    "List the current routing table: each category with its target_class, model, and "
    "description. Read-only — use it to see what you're editing."
)


@dataclass
class RoutingAdminService:
    """Builds the owner session's ``chief_routing`` MCP server (the self-config edits).

    Owner-only (never wired into a guest session), mirroring
    :class:`~chief.tools.web.WebService`: ``tasks.py`` registers :meth:`server_config`
    in the owner ``mcp_servers`` mapping, which reaches both backends via the #80
    adapter. Holds the shared :class:`~chief.core.routing.RoutingStore` — the same
    instance the task engine resolves against — so an edit is live immediately.
    """

    routing: RoutingStore
    server_name: str = SERVER_NAME

    def _build_set_target_tool(self) -> InProcessTool:
        routing = self.routing

        @tool(
            "set_target",
            _SET_TARGET_DESCRIPTION,
            {"category": str, "target_class": str, "model": str},
        )
        async def set_target(args: dict[str, Any]) -> dict[str, Any]:
            category = str(args.get("category", "")).strip()
            target_class = str(args.get("target_class", "")).strip()
            model = str(args.get("model", "")).strip()
            try:
                await routing.set_target(
                    category, target_class=target_class, model=model
                )
            except RoutingEditError as exc:
                return _text_result(f"Could not change target: {exc}", is_error=True)
            return _text_result(
                f"Routing category {category!r} now targets "
                f"{target_class}:{model}."
            )

        return set_target

    def _build_add_category_tool(self) -> InProcessTool:
        routing = self.routing

        @tool(
            "add_category",
            _ADD_CATEGORY_DESCRIPTION,
            {
                "category": str,
                "target_class": str,
                "model": str,
                "description": str,
            },
        )
        async def add_category(args: dict[str, Any]) -> dict[str, Any]:
            category = str(args.get("category", "")).strip()
            target_class = str(args.get("target_class", "")).strip()
            model = str(args.get("model", "")).strip()
            description = str(args.get("description", "")).strip() or None
            try:
                await routing.add_category(
                    category,
                    target_class=target_class,
                    model=model,
                    description=description,
                )
            except RoutingEditError as exc:
                return _text_result(f"Could not add category: {exc}", is_error=True)
            return _text_result(
                f"Added category {category!r} → {target_class}:{model}."
            )

        return add_category

    def _build_remove_category_tool(self) -> InProcessTool:
        routing = self.routing

        @tool("remove_category", _REMOVE_CATEGORY_DESCRIPTION, {"category": str})
        async def remove_category(args: dict[str, Any]) -> dict[str, Any]:
            category = str(args.get("category", "")).strip()
            try:
                await routing.remove_category(category)
            except RoutingEditError as exc:
                return _text_result(
                    f"Could not remove category: {exc}", is_error=True
                )
            return _text_result(f"Removed category {category!r}.")

        return remove_category

    def _build_rename_category_tool(self) -> InProcessTool:
        routing = self.routing

        @tool(
            "rename_category",
            _RENAME_CATEGORY_DESCRIPTION,
            {"old": str, "new": str},
        )
        async def rename_category(args: dict[str, Any]) -> dict[str, Any]:
            old = str(args.get("old", "")).strip()
            new = str(args.get("new", "")).strip()
            try:
                await routing.rename_category(old, new)
            except RoutingEditError as exc:
                return _text_result(
                    f"Could not rename category: {exc}", is_error=True
                )
            return _text_result(f"Renamed category {old!r} → {new!r}.")

        return rename_category

    def _build_describe_category_tool(self) -> InProcessTool:
        routing = self.routing

        @tool(
            "describe_category",
            _DESCRIBE_CATEGORY_DESCRIPTION,
            {"category": str, "description": str},
        )
        async def describe_category(args: dict[str, Any]) -> dict[str, Any]:
            category = str(args.get("category", "")).strip()
            description = str(args.get("description", "")).strip() or None
            try:
                await routing.set_description(category, description)
            except RoutingEditError as exc:
                return _text_result(
                    f"Could not set description: {exc}", is_error=True
                )
            if description is None:
                return _text_result(f"Cleared the description for {category!r}.")
            return _text_result(f"Set the description for {category!r}.")

        return describe_category

    def _build_list_routing_tool(self) -> InProcessTool:
        routing = self.routing

        @tool("list_routing", _LIST_ROUTING_DESCRIPTION, {})
        async def list_routing(_: dict[str, Any]) -> dict[str, Any]:
            descriptions = routing.descriptions()
            lines: list[str] = []
            for category in routing.categories():
                target = routing.resolve(category)
                assert target is not None  # a listed category always has a row
                blurb = descriptions.get(category)
                suffix = f" — {blurb}" if blurb else ""
                lines.append(
                    f"{category}: {target.target_class}:{target.model}{suffix}"
                )
            body = "\n".join(lines) if lines else "(the routing table is empty)"
            return _text_result(body)

        return list_routing

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the routing-admin tools."""
        return create_sdk_mcp_server(
            self.server_name,
            tools=[
                self._build_set_target_tool(),
                self._build_add_category_tool(),
                self._build_remove_category_tool(),
                self._build_rename_category_tool(),
                self._build_describe_category_tool(),
                self._build_list_routing_tool(),
            ],
        )
