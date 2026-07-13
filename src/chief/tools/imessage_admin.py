"""The owner's iMessage whitelist tools (#156) — "chief, listen to Mom".

One in-process server (``chief_imessage_admin``), wired into **owner** sessions
only (the same tier isolation as :class:`~chief.tools.guest.GuestAdminService`),
so the owner manages the whitelist in plain language from any surface:

- ``manage_imessage_handle`` — allow (add as guest; the owner's add IS the
  admission), remove, promote to draft-first, or back to auto.
- ``list_imessage_handles`` — the whitelist with tier + delegation mode.
- ``unknown_imessage_senders`` — "who's texted you?": the metadata-only log of
  non-whitelisted senders (handle + timestamps; their content was never read).

All three are owner-initiated and reversible → pre-approved (no card), exactly
like ``manage_guest``.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..persistence import imessage as repo
from .inprocess import (
    InProcessServerConfig,
    InProcessTool,
    create_sdk_mcp_server,
    tool,
)

SERVER_NAME = "chief_imessage_admin"

_MANAGE_DESCRIPTION = (
    "Manage the iMessage whitelist by handle (an E.164 phone number like "
    "+15551234567, or an email). Actions: 'allow' adds the handle as a guest — "
    "it can then text chief directly (auto mode); 'draft' switches its "
    "conversation to draft-first (every outbound text needs the owner's "
    "approval); 'auto' switches back to free replies; 'remove' takes the handle "
    "off the whitelist entirely (its texts go back to the unknown-senders log). "
    "Only whitelisted handles ever reach chief."
)
_LIST_DESCRIPTION = (
    "List every whitelisted iMessage handle with its tier (owner/guest) and its "
    "conversation's delegation mode (auto or draft-first)."
)
_UNKNOWN_DESCRIPTION = (
    "Who has texted chief's number without being whitelisted? Returns handles "
    "with first/last seen times and a message count — their content was never "
    "read. Use manage_imessage_handle to whitelist one."
)

_MANAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "handle": {
            "type": "string",
            "description": "Phone (E.164, e.g. +15551234567) or email.",
        },
        "action": {
            "type": "string",
            "description": "allow | remove | draft | auto",
        },
    },
    "required": ["handle", "action"],
}
_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {},
                                 "required": []}

_ACTIONS = ("allow", "remove", "draft", "auto")


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


@dataclass(frozen=True)
class IMessageAdminService:
    """Builds the owner session's iMessage whitelist server (three tools)."""

    session_factory: async_sessionmaker[AsyncSession]
    server_name: str = SERVER_NAME

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The SDK-qualified tool names (for allow-lists)."""
        return (
            f"mcp__{self.server_name}__manage_imessage_handle",
            f"mcp__{self.server_name}__list_imessage_handles",
            f"mcp__{self.server_name}__unknown_imessage_senders",
        )

    def _build_manage(self) -> InProcessTool:
        session_factory = self.session_factory

        @tool("manage_imessage_handle", _MANAGE_DESCRIPTION, _MANAGE_SCHEMA)
        async def manage_imessage_handle(args: dict[str, Any]) -> dict[str, Any]:
            handle = repo.normalize_handle(str(args.get("handle", "")))
            action = str(args.get("action", "")).strip().lower()
            if action not in _ACTIONS:
                return _text_result(
                    f"Unknown action '{action}'. Use allow, remove, draft, "
                    "or auto.",
                    is_error=True,
                )
            if not handle:
                return _text_result(
                    "Which handle? Give a phone number (+15551234567) or email.",
                    is_error=True,
                )
            async with session_factory() as session:
                if action == "allow":
                    await repo.add_handle(
                        session, handle=handle, tier=repo.TIER_GUEST
                    )
                    return _text_result(
                        f"Done — {handle} is whitelisted as a guest (auto mode). "
                        "It can text chief now, within guest limits."
                    )
                if action == "remove":
                    removed = await repo.remove_handle(session, handle)
                    return _text_result(
                        f"Done — {handle} is off the whitelist."
                        if removed
                        else f"{handle} wasn't on the whitelist."
                    )
                mode = (
                    repo.MODE_DRAFT if action == "draft" else repo.MODE_AUTO
                )
                pref = await repo.set_mode(session, handle, mode)
                if pref is None:
                    return _text_result(
                        f"{handle} isn't whitelisted — allow it first.",
                        is_error=True,
                    )
                return _text_result(
                    f"Done — {handle} is now "
                    + (
                        "draft-first: every outbound text needs your approval."
                        if mode == repo.MODE_DRAFT
                        else "auto: chief replies freely within guest limits."
                    )
                )

        return manage_imessage_handle

    def _build_list(self) -> InProcessTool:
        session_factory = self.session_factory

        @tool("list_imessage_handles", _LIST_DESCRIPTION, _EMPTY_SCHEMA)
        async def list_imessage_handles(args: dict[str, Any]) -> dict[str, Any]:
            async with session_factory() as session:
                entries = await repo.list_whitelist(session)
            if not entries:
                return _text_result("The iMessage whitelist is empty.")
            lines = [
                f"• {contact.user_id} — {contact.tier}"
                + (
                    f", {pref.mode}"
                    if pref is not None and contact.tier == repo.TIER_GUEST
                    else ""
                )
                + (f" ({contact.state})" if contact.state != "admitted" else "")
                for contact, pref in entries
            ]
            return _text_result("\n".join(lines))

        return list_imessage_handles

    def _build_unknown(self) -> InProcessTool:
        session_factory = self.session_factory

        @tool("unknown_imessage_senders", _UNKNOWN_DESCRIPTION, _EMPTY_SCHEMA)
        async def unknown_imessage_senders(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            async with session_factory() as session:
                rows = await repo.list_unknown_senders(
                    session, platform=repo.PLATFORM
                )
            if not rows:
                return _text_result("Nobody unknown has texted.")
            lines = [
                f"• {row.handle} — {row.count} message(s), "
                f"last {row.last_seen:%Y-%m-%d %H:%M} UTC"
                for row in rows
            ]
            return _text_result("\n".join(lines))

        return unknown_imessage_senders

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the whitelist tools."""
        return create_sdk_mcp_server(
            self.server_name,
            tools=[
                self._build_manage(),
                self._build_list(),
                self._build_unknown(),
            ],
        )
