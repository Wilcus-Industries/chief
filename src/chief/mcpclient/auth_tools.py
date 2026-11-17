"""The owner's way into an MCP login: two tools, two slash commands, one hint.

``server_login`` / ``server_login_complete`` let the model act on "connect
robinhood". ``/login`` and ``/logout`` do the same with no model call, so an
address the owner pastes back never enters a transcript. A pre-turn hook tells
the model which servers are waiting. The login link itself is sent straight to
the asking conversation by the broker; the model is only ever told it went out.
"""

from pathlib import Path
from typing import Protocol

from chief.adapters.base import Adapter, Message
from chief.audit import AuditLog
from chief.commands import CommandHandler
from chief.config import Config
from chief.hooks.context import TurnContext
from chief.hooks.registry import HookRegistry
from chief.mcpclient.oauth import OAuthBroker
from chief.mcpclient.oauth_types import CALLBACK_PATH, REDIRECT_HOST, Surface
from chief.owner import primary_target
from chief.provider.base import ToolSpec
from chief.tools import Tool, ToolContext, ToolRegistry

TOKEN_DIR = Path("secrets/mcp-oauth")
HOOK_NAME = "core-mcp-auth"
LOGIN_USAGE = "usage: /login <server>  or  /login <server> <address you were sent to>"
LOGOUT_USAGE = "usage: /logout <server>"

_LOGIN = ToolSpec(
    name="server_login",
    description=(
        "Start the OAuth login for an MCP server that needs one (the context "
        "lists those). Sends the owner a login link on THIS conversation — you "
        "never see the link. Call it when the owner asks to connect or use that "
        "server. They approve it in a browser and send back the address the "
        "browser ends up on (it starts with http://127.0.0.1); then call "
        "server_login_complete with that address exactly as sent."
    ),
    parameters={
        "type": "object",
        "properties": {"server": {"type": "string"}},
        "required": ["server"],
    },
)
_COMPLETE = ToolSpec(
    name="server_login_complete",
    description=(
        "Finish an MCP server login with the address the owner sent back after "
        "approving it (it starts with http://127.0.0.1). Pass `callback_url` "
        "exactly as sent."
    ),
    parameters={
        "type": "object",
        "properties": {
            "server": {"type": "string"},
            "callback_url": {"type": "string"},
        },
        "required": ["server", "callback_url"],
    },
)


class Sender(Protocol):
    """What the broker needs of the dispatcher: an adapter per channel."""

    def adapter(self, channel: str) -> Adapter: ...


class CommandRegistry(Protocol):
    def register(self, name: str, handler: CommandHandler) -> None: ...


def build_broker(config: Config, dispatcher: Sender, audit: AuditLog) -> OAuthBroker:
    """The broker for ``config``: links and notices go out through the
    dispatcher's adapters, the redirect points back at the web UI's callback."""

    async def send(surface: Surface, text: str) -> None:
        await dispatcher.adapter(surface[0]).send(surface[1], text)

    return OAuthBroker(
        send=send,
        audit=audit,
        notice_to=primary_target(config),
        redirect_uri=f"http://{REDIRECT_HOST}:{config.web_port}{CALLBACK_PATH}",
        token_dir=TOKEN_DIR,
        notified_path=config.db_path.parent / "mcp-oauth-notified.json",
    )


def register_auth_surface(
    broker: OAuthBroker,
    registry: ToolRegistry,
    commands: CommandRegistry,
    hooks: HookRegistry,
) -> None:
    """Expose ``broker`` as tools, slash commands and a pre-turn hint."""

    async def server_login(server: str, context: ToolContext | None = None) -> str:
        if context is None:
            return "error: server_login needs a session context"
        return await broker.start_login(server, (context.channel, context.thread_key))

    async def server_login_complete(server: str, callback_url: str) -> str:
        return (await broker.complete_login(callback_url, server)).message

    registry.register(Tool(_LOGIN, server_login, wants_context=True))
    registry.register(Tool(_COMPLETE, server_login_complete))

    async def login(args: str, message: Message) -> str:
        parts = args.split()
        if len(parts) == 1:
            return await broker.start_login(
                parts[0], (message.channel, message.thread_key)
            )
        if len(parts) == 2:
            return (await broker.complete_login(parts[1], parts[0])).message
        return LOGIN_USAGE

    async def logout(args: str, message: Message) -> str:
        parts = args.split()
        return await broker.logout(parts[0]) if len(parts) == 1 else LOGOUT_USAGE

    commands.register("login", login)
    commands.register("logout", logout)

    async def waiting(_turn: TurnContext) -> str | None:
        names = broker.needs_auth()
        if not names:
            return None
        return (
            f"MCP servers needing login: {', '.join(names)} "
            "(call server_login when the owner asks to use one)"
        )

    hooks.register_pre_turn(HOOK_NAME, waiting)
