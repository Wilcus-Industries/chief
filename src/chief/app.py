"""Application wiring: build the whole daemon from a Config.

Tests boot exactly this wiring with only the provider swapped for the
deterministic fake (the one scripted fake CI allows, PRD #183).
"""

import logging
import sys
from pathlib import Path

from chief.adapters.imessage import IMessageAdapter
from chief.adapters.socket import SocketAdapter
from chief.agent.compaction import Compactor
from chief.agent.manager import SessionManager
from chief.agent.prompt import ONBOARDING_SUFFIX, system_prompt
from chief.agent.tools import ToolContext, ToolDispatcher, ToolRegistry
from chief.approvals import Approval, ApprovalBroker
from chief.audit import AuditLog
from chief.budget import Budget
from chief.bus import EventBus
from chief.commands import CommandSet
from chief.config import Config
from chief.cron.service import CronService
from chief.cron.timing import parse_quiet_hours
from chief.cron.tools import register_cron_tools
from chief.daemon import App
from chief.dispatch import Dispatcher
from chief.gate import GatedTools, GatePolicy, load_approved, save_approved
from chief.mcpclient.manager import McpManager, ServerConfig
from chief.mcpclient.tools import load_self_added, register_mcp_tools
from chief.monitors.service import ModelJudge, MonitorService
from chief.monitors.tools import register_monitor_tools
from chief.packages import (
    CLONED_PACKAGES_DIR,
    PackageLibrary,
    register_package_tools,
)
from chief.persistence.db import init_schema, make_engine, make_session_factory
from chief.persistence.store import MessageStore
from chief.provider.base import Provider
from chief.provider.openrouter import OpenRouterProvider
from chief.selfedit.pipeline import SelfEditPipeline
from chief.selfedit.recovery import RestartController
from chief.selfedit.tools import register_install_tool, register_selfedit_tools
from chief.skills import SkillLibrary, register_skill_tools
from chief.strangers import StrangerLog
from chief.subagents import AgentRegistry, register_spawn_tool
from chief.web.adapter import WebAdapter
from chief.web.app import build_web_app
from chief.web.auth import Auth, load_or_create_secret
from chief.web.server import WebServer

logger = logging.getLogger(__name__)

__all__ = ["App", "build_app"]


async def build_app(config: Config, provider: Provider | None = None) -> App:
    """Assemble every core service; ``provider`` overrides OpenRouter (tests)."""
    engine = make_engine(config.db_path)
    await init_schema(engine)
    factory = make_session_factory(engine)
    store = MessageStore(factory)
    provider = provider or OpenRouterProvider(config.openrouter_api_key)

    audit = AuditLog(config.db_path.parent / "audit.jsonl")
    config_approved = set(config.gate_approved)
    approved_path = config.db_path.parent / "gate_approved.json"
    policy = GatePolicy(
        never=frozenset(config.gate_never),
        approved=config_approved | load_approved(approved_path),
    )

    def allow_always(tool_name: str) -> None:
        policy.allow_always(tool_name)
        save_approved(policy.approved - config_approved, approved_path)

    approvals = ApprovalBroker()
    budget = Budget(factory, config.budget_cap_usd, config.budget_warn_ratio)
    bus = EventBus()
    registry = ToolRegistry()

    def tools_factory(thread_key: str, channel: str) -> ToolDispatcher:
        context = ToolContext(thread_key=thread_key, channel=channel)

        async def ask(ctx: ToolContext, question: str) -> Approval:
            send = dispatcher.adapter(ctx.channel).send
            return await approvals.ask(
                ctx.thread_key, question, lambda q: send(ctx.thread_key, q)
            )

        return GatedTools(
            registry=registry,
            policy=policy,
            audit=audit,
            context=context,
            ask=ask,
            on_always=allow_always,
        )

    skills = SkillLibrary(config.skills_dir)
    prompt = system_prompt() + skills.prompt_lines()
    if not await store.has_sessions():
        prompt += ONBOARDING_SUFFIX
    restart_controller = RestartController()
    manager = SessionManager(
        provider=provider,
        tools_factory=tools_factory,
        store=store,
        default_model=config.default_model,
        system_prompt=prompt,
        max_concurrent=config.max_concurrent_sessions,
        budget=budget,
        downgrade_model=config.models.get("downgrade"),
        compactor=Compactor(provider, config.default_model),
        after_commit=restart_controller.fire_if_requested,
    )
    dispatcher = Dispatcher(
        manager, bus=bus, approvals=approvals, strangers=StrangerLog(factory)
    )
    judge = ModelJudge(
        provider, config.models.get("cheap-judgment", config.default_model)
    )
    monitor_service = MonitorService(factory, bus, dispatcher.handle, judge)
    cron_service = CronService(
        factory, dispatcher.handle, parse_quiet_hours(config.quiet_hours)
    )
    register_monitor_tools(registry, monitor_service)
    register_cron_tools(registry, cron_service)
    selfedit_pipeline = SelfEditPipeline(Path.cwd(), audit, restart_controller.request)
    register_selfedit_tools(registry, selfedit_pipeline)
    mcp_manager = McpManager(registry)
    register_mcp_tools(registry, mcp_manager, audit)
    mcp_configs = tuple(
        ServerConfig(
            name=name,
            url=entry.get("url"),
            command=tuple(entry["command"]) if entry.get("command") else None,
        )
        for name, entry in config.mcp_servers.items()
    ) + tuple(load_self_added())
    register_skill_tools(registry, skills)
    package_library = PackageLibrary((config.packages_dir, CLONED_PACKAGES_DIR))
    register_package_tools(registry, package_library, config.packages_repo)
    register_install_tool(registry, selfedit_pipeline, package_library)
    register_spawn_tool(
        registry,
        AgentRegistry(config.agents_dir),
        provider,
        config.default_model,
        budget,
    )
    command_set = CommandSet(
        manager, monitor_service, cron_service, skills=skills
    )
    dispatcher.set_commands(command_set)

    socket_adapter = SocketAdapter(config.socket_path, dispatcher.handle)
    dispatcher.register(socket_adapter)

    imessage_adapter: IMessageAdapter | None = None
    if config.imessage_enabled and sys.platform == "darwin":
        imessage_adapter = IMessageAdapter(
            dispatcher.handle,
            db_path=config.imessage_db_path,
            cursor_path=config.db_path.parent / "imessage_cursor",
            owner_handles=config.imessage_owner_handles,
            poll_seconds=config.imessage_poll_seconds,
        )
        dispatcher.register(imessage_adapter)

    web_adapter = WebAdapter()
    dispatcher.register(web_adapter)
    web_server: WebServer | None = None
    if config.web_password:
        web_app = build_web_app(
            Auth(config.web_password, load_or_create_secret()),
            web_adapter,
            dispatcher.handle,
            monitor_service,
            store,
            command_set.palette,
        )
        web_server = WebServer(web_app, config.web_host, config.web_port)

    return App(
        config=config,
        engine=engine,
        store=store,
        dispatcher=dispatcher,
        socket_adapter=socket_adapter,
        monitor_service=monitor_service,
        cron_service=cron_service,
        bus=bus,
        web_adapter=web_adapter,
        web_server=web_server,
        mcp_manager=mcp_manager,
        mcp_configs=mcp_configs,
        imessage_adapter=imessage_adapter,
    )
