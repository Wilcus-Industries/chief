"""The guest receptionist's in-process tools (M6).

Two SDK MCP tools on **separate servers**, so a guest session can wire the guest server
without ever inheriting the owner's admin tool:

- :class:`GuestService` (server ``chief_guest``) — ``leave_message``, the only effectful
  thing a guest can do without approval. It relays the guest's note to the owner's Front
  Desk. The sender label and the relay callback are baked into the closure per session
  (mirroring how :class:`~chief.tools.shell.ShellService` bakes the task's shell key),
  so the model cannot spoof the sender and the module stays free of adapter imports.
- :class:`GuestAdminService` (server ``chief_guest_admin``) — ``manage_guest``, wired
  into **owner** sessions only. It lets the owner block/mute/unblock a guest by name in
  plain language; the model resolves the name and this tool sets the admission state.

Availability and booking are not in this module — they reuse the narrowed calendar MCP
(``tools.calendar.mcp.guest_service``): free/busy reads ALLOW, ``create-event`` routes
to the owner's approval card on the Front Desk.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..persistence import contacts as contact_repo
from .inprocess import (
    InProcessServerConfig,
    InProcessTool,
    create_sdk_mcp_server,
    tool,
)

#: Relays a formatted guest note to the owner's Front Desk (built per session).
Relay = Callable[[str], Awaitable[None]]

_LEAVE_MESSAGE_DESCRIPTION = (
    "Pass the visitor's message along to the owner. Use this whenever the visitor "
    "wants to leave a note, ask the owner something, or get a reply. The owner sees it "
    "at their Front Desk. The visitor's identity is attached for you."
)

_MANAGE_GUEST_DESCRIPTION = (
    "Block, mute, or unblock a guest by name. 'block' ignores them entirely; 'mute' "
    "keeps taking their messages silently (no reply); 'unblock' restores service. "
    "If the name matches more than one guest, you'll get the list back to disambiguate."
)

_ACTION_STATES = {
    "block": contact_repo.STATE_BLOCKED,
    "mute": contact_repo.STATE_MUTED,
    "unblock": contact_repo.STATE_ADMITTED,
}


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


@dataclass(frozen=True)
class GuestService:
    """Builds a guest session's in-process ``leave_message`` tool (relay baked in)."""

    relay: Relay
    from_label: str
    server_name: str = "chief_guest"

    @property
    def tool_name(self) -> str:
        """The SDK-qualified ``mcp__chief_guest__leave_message`` name (allow-list)."""
        return f"mcp__{self.server_name}__leave_message"

    def _build_tool(self) -> InProcessTool:
        relay, from_label = self.relay, self.from_label

        @tool("leave_message", _LEAVE_MESSAGE_DESCRIPTION, {"message": str})
        async def leave_message(args: dict[str, Any]) -> dict[str, Any]:
            message = str(args.get("message", "")).strip()
            if not message:
                return _text_result("No message to pass along.", is_error=True)
            await relay(f"📨 Message from {from_label}:\n\n{message}")
            return _text_result("Your message has been passed along to the owner.")

        return leave_message

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for this guest session's relay tool."""
        return create_sdk_mcp_server(self.server_name, tools=[self._build_tool()])


@dataclass(frozen=True)
class GuestAdminService:
    """Builds the owner session's ``manage_guest`` tool (block/mute/unblock by name)."""

    session_factory: async_sessionmaker[AsyncSession]
    platform: str
    server_name: str = "chief_guest_admin"

    @property
    def tool_name(self) -> str:
        """The SDK-qualified ``mcp__chief_guest_admin__manage_guest`` name."""
        return f"mcp__{self.server_name}__manage_guest"

    def _build_tool(self) -> InProcessTool:
        session_factory, platform = self.session_factory, self.platform

        @tool(
            "manage_guest",
            _MANAGE_GUEST_DESCRIPTION,
            {"name": str, "action": str},
        )
        async def manage_guest(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args.get("name", "")).strip()
            action = str(args.get("action", "")).strip().lower()
            if action not in _ACTION_STATES:
                return _text_result(
                    f"Unknown action '{action}'. Use block, mute, or unblock.",
                    is_error=True,
                )
            if not name:
                return _text_result("Which guest? Give a name.", is_error=True)

            async with session_factory() as session:
                matches = await contact_repo.find_contacts_by_name(
                    session, platform=platform, name=name
                )
                if not matches:
                    return _text_result(f"No guest matching '{name}'.")
                if len(matches) > 1:
                    listing = ", ".join(
                        f"{c.display_name} ({c.namespace})" for c in matches
                    )
                    return _text_result(
                        f"Several guests match '{name}': {listing}. "
                        "Be more specific (the full name)."
                    )
                contact = matches[0]
                await contact_repo.set_contact_state(
                    session, contact, _ACTION_STATES[action]
                )
            return _text_result(f"Done — {contact.display_name} is now {action}ed.")

        return manage_guest

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the owner's guest-admin tool."""
        return create_sdk_mcp_server(self.server_name, tools=[self._build_tool()])
