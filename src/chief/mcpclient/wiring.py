"""Boot phase for MCP: the server configs, the OAuth broker, and the manager.

Kept out of ``chief.wiring`` so that file stays under the 200-line cap, the way
``chief.peers.wiring`` keeps messaging out of ``chief.app``."""

from chief.audit import AuditLog
from chief.commands import CommandSet
from chief.config import Config
from chief.mcpclient.auth_tools import build_broker, register_auth_surface
from chief.mcpclient.config import ServerConfig, server_config_from_entry
from chief.mcpclient.manager import McpManager
from chief.mcpclient.oauth import OAuthBroker
from chief.wiring import Core


def build_mcp_configs(config: Config) -> tuple[ServerConfig, ...]:
    """The ``mcp_servers`` entries as server configs. An entry with a malformed
    ``timeout``, ``headers``, ``auth``, ``scopes`` or tool filter
    (``include_tools`` / ``exclude_tools``) — or a ``${secret:}`` that
    resolves to nothing — is dropped (logged, not raised) by
    ``server_config_from_entry``: see there for why."""
    return tuple(
        sc
        for name, entry in config.mcp_servers.items()
        if (sc := server_config_from_entry(name, entry)) is not None
    )


def build_mcp(
    config: Config, core: Core, commands: CommandSet, audit: AuditLog
) -> tuple[McpManager, tuple[ServerConfig, ...], OAuthBroker]:
    """MCP servers are pure config: the agent adds one by editing the
    ``mcp_servers`` key (then ``restart``), and it connects on the next boot.

    An ``auth: oauth`` server logs in through the broker built here. Its
    ``server_login`` tools, ``/login`` ``/logout`` commands and "needs login"
    hint are registered on the core's registry, commands and hooks only when some
    server needs them — an install without one keeps its toolset as it was.
    """
    broker = build_broker(config, core.dispatcher, audit)
    mcp_configs = build_mcp_configs(config)
    if any(sc.auth == "oauth" for sc in mcp_configs):
        register_auth_surface(broker, core.registry, commands, core.hooks)
    return McpManager(core.registry, broker), mcp_configs, broker
