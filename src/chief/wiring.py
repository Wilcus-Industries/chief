"""Boot phases for ``build_app``: the infrastructure around the agent core.

Persistence, the gate/approval layer, MCP config, and the channel adapters —
each a small helper so ``chief.app.build_app`` reads as a table of contents.
The agent core (the manager/dispatcher/tools_factory closure trap) and the
default toolset live next door in ``chief.app`` and ``chief.tools.native``.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.imessage import IMessageAdapter
from chief.adapters.socket import SocketAdapter
from chief.agent.manager import SessionManager
from chief.approvals import ApprovalBroker
from chief.audit import AuditLog
from chief.budget import Budget
from chief.bus import EventBus
from chief.commands import CommandSet
from chief.config import Config
from chief.cron.service import CronService
from chief.dispatch import Dispatcher
from chief.gate_policy import GatePolicy, load_approved, save_approved
from chief.hub import ObserverHub
from chief.mcpclient.manager import McpManager, ServerConfig, server_config_from_entry
from chief.monitors.service import MonitorService
from chief.persistence.db import (
    SessionFactory,
    init_schema,
    make_engine,
    make_session_factory,
)
from chief.persistence.store import MessageStore
from chief.provider.base import Provider
from chief.provider.openrouter import OpenRouterProvider
from chief.provider.router import RouterProvider
from chief.selfedit.recovery import RestartController
from chief.tools import ToolRegistry
from chief.web.adapter import WebAdapter
from chief.web.app import build_web_app
from chief.web.auth import Auth, load_or_create_secret
from chief.web.server import WebServer


@dataclass
class Gate:
    """The gate/approval layer: audit trail, policy, its persister, broker."""

    audit: AuditLog
    policy: GatePolicy
    allow_always: Callable[[str], None]
    approvals: ApprovalBroker


@dataclass
class Core:
    """The agent core services build_app threads into the toolset and App."""

    budget: Budget
    bus: EventBus
    hub: ObserverHub
    registry: ToolRegistry
    restart: RestartController
    manager: SessionManager
    dispatcher: Dispatcher
    monitors: MonitorService
    cron: CronService


@dataclass
class Adapters:
    """The channel adapters registered on the dispatcher, plus the web server."""

    socket: SocketAdapter
    imessage: IMessageAdapter | None
    web_adapter: WebAdapter
    web_server: WebServer | None


def build_provider(config: Config) -> Provider:
    """The LLM provider: a RouterProvider over named backends when any are
    configured, else the single legacy OpenRouter/default backend.

    Every backend is an OpenAI-compatible ``OpenRouterProvider`` with its own
    base_url + key. Fails LOUD at boot (not on the first turn) if an alias names
    a backend that was never assembled.
    """
    backends: dict[str, Provider] = {
        name: OpenRouterProvider(
            spec.api_key, temperature=config.temperature, base_url=spec.base_url
        )
        for name, spec in config.provider_backends.items()
    }
    default = backends.get("default") or OpenRouterProvider(
        config.openrouter_api_key,
        temperature=config.temperature,
        base_url=config.provider_base_url,
    )
    if not config.provider_backends and not config.provider_aliases:
        return default
    backends.setdefault("default", default)
    aliases: dict[str, tuple[str, str]] = {}
    for name, alias in config.provider_aliases.items():
        if alias.backend not in backends:
            raise ValueError(
                f"provider alias '{name}' names unknown backend '{alias.backend}'"
            )
        aliases[name] = (alias.backend, alias.model)
    return RouterProvider(default=default, backends=backends, aliases=aliases)


async def build_persistence(
    config: Config,
) -> tuple[AsyncEngine, SessionFactory, MessageStore]:
    """Engine, schema, session factory, store — the durable layer."""
    engine = make_engine(config.db_path)
    await init_schema(engine)
    factory = make_session_factory(engine)
    return engine, factory, MessageStore(factory)


def build_gate(config: Config) -> Gate:
    """Audit log, gate policy, its always-allow persister, and the broker."""
    audit = AuditLog(config.db_path.parent / "audit.jsonl")
    config_approved = set(config.gate_approved)
    approved_path = config.db_path.parent / "gate_approved.json"
    policy = GatePolicy(
        never=frozenset(config.gate_never),
        approved=config_approved | load_approved(approved_path),
        ask_when=config.gate_ask_when,
    )

    def allow_always(tool_name: str) -> None:
        policy.allow_always(tool_name)
        save_approved(policy.approved - config_approved, approved_path)

    return Gate(audit, policy, allow_always, ApprovalBroker())


def build_mcp(
    config: Config, registry: ToolRegistry
) -> tuple[McpManager, tuple[ServerConfig, ...]]:
    """MCP servers are pure config: the agent adds one by editing the
    ``mcp_servers`` key (then ``restart``), and it connects on the next boot.

    An entry with a malformed ``timeout`` is dropped (logged, not raised) by
    ``server_config_from_entry`` — see there for why.
    """
    mcp_configs = tuple(
        sc
        for name, entry in config.mcp_servers.items()
        if (sc := server_config_from_entry(name, entry)) is not None
    )
    return McpManager(registry), mcp_configs


def build_adapters(
    config: Config, core: Core, store: MessageStore, commands: CommandSet,
    approvals: ApprovalBroker,
) -> Adapters:
    """Register the socket, darwin-gated iMessage, and web adapters; build the
    optional password-gated web server."""
    dispatcher = core.dispatcher
    socket_adapter = SocketAdapter(config.socket_path, dispatcher.handle)
    dispatcher.register(socket_adapter)

    imessage_adapter: IMessageAdapter | None = None
    if config.imessage_enabled and sys.platform == "darwin":
        imessage_adapter = IMessageAdapter(
            # fire_restart=False: adapter fires it after its cursor is durable.
            lambda m: dispatcher.handle(m, fire_restart=False),
            db_path=config.imessage_db_path, self_handles=config.imessage_self_handles,
            cursor_path=config.db_path.parent / "imessage_cursor",
            owner_db_path=config.imessage_owner_db_path,
            owner_handles=config.imessage_owner_handles,
            poll_seconds=config.imessage_poll_seconds,
            restart=core.restart, dedicated=config.imessage_dedicated,
            # Consume approvals at poll stage, ahead of the thread FIFO worker.
            resolve_approval=dispatcher.resolve_approval,
        )
        dispatcher.register(imessage_adapter)

    # Web-origin turns stream to the hub; other channels reach it as ticks.
    web_adapter = WebAdapter(core.hub)
    dispatcher.register(web_adapter)
    web_server: WebServer | None = None
    if config.web_password:
        web_app = build_web_app(
            Auth(config.web_password, load_or_create_secret()), web_adapter, core.hub,
            dispatcher.handle, core.monitors, store, commands.palette, core.manager,
            approvals, config.stream_channel_defaults, config.imessage_owner_handles)
        web_server = WebServer(web_app, config.web_host, config.web_port)
    return Adapters(socket_adapter, imessage_adapter, web_adapter, web_server)
