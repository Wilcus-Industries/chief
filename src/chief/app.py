"""Application wiring: build the whole daemon from a Config.

Tests boot exactly this wiring with only the provider swapped for the
deterministic fake (the one scripted fake CI allows, PRD #183).
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.socket import SocketAdapter
from chief.agent.manager import SessionManager
from chief.agent.prompt import ONBOARDING_SUFFIX, system_prompt
from chief.agent.tools import ToolContext, ToolDispatcher, ToolRegistry
from chief.approvals import ApprovalBroker
from chief.audit import AuditLog
from chief.budget import Budget
from chief.bus import EventBus
from chief.commands import CommandSet
from chief.config import Config
from chief.cron.service import CronService
from chief.cron.timing import parse_quiet_hours
from chief.cron.tools import register_cron_tools
from chief.dispatch import Dispatcher
from chief.gate import GatedTools, GatePolicy
from chief.monitors.service import ModelJudge, MonitorService
from chief.monitors.tools import register_monitor_tools
from chief.persistence.db import init_schema, make_engine, make_session_factory
from chief.persistence.store import MessageStore
from chief.provider.base import Provider
from chief.provider.openrouter import OpenRouterProvider
from chief.selfedit.pipeline import SelfEditPipeline
from chief.selfedit.recovery import restart_daemon
from chief.selfedit.tools import register_selfedit_tools
from chief.strangers import StrangerLog
from chief.web.adapter import WebAdapter
from chief.web.app import build_web_app
from chief.web.auth import Auth
from chief.web.server import WebServer


@dataclass
class App:
    """The assembled daemon and its lifecycle."""

    config: Config
    engine: AsyncEngine
    store: MessageStore
    dispatcher: Dispatcher
    socket_adapter: SocketAdapter
    monitor_service: MonitorService
    cron_service: CronService
    bus: EventBus
    web_adapter: WebAdapter
    web_server: WebServer | None

    async def start(self) -> None:
        await self.socket_adapter.start()
        self.cron_service.start()
        if self.web_server is not None:
            await self.web_server.start()

    async def stop(self) -> None:
        if self.web_server is not None:
            await self.web_server.stop()
        await self.cron_service.stop()
        await self.web_adapter.stop()
        await self.socket_adapter.stop()
        await self.engine.dispose()


async def build_app(config: Config, provider: Provider | None = None) -> App:
    """Assemble every core service; ``provider`` overrides OpenRouter (tests)."""
    engine = make_engine(config.db_path)
    await init_schema(engine)
    factory = make_session_factory(engine)
    store = MessageStore(factory)
    provider = provider or OpenRouterProvider(config.openrouter_api_key)

    audit = AuditLog(config.db_path.parent / "audit.jsonl")
    policy = GatePolicy(
        never=frozenset(config.gate_never), approved=frozenset(config.gate_approved)
    )
    approvals = ApprovalBroker()
    budget = Budget(factory, config.budget_cap_usd, config.budget_warn_ratio)
    bus = EventBus()
    registry = ToolRegistry()

    def tools_factory(thread_key: str, channel: str) -> ToolDispatcher:
        context = ToolContext(thread_key=thread_key, channel=channel)

        async def ask(ctx: ToolContext, question: str) -> bool:
            send = dispatcher.adapter(ctx.channel).send
            return await approvals.ask(
                ctx.thread_key, question, lambda q: send(ctx.thread_key, q)
            )

        return GatedTools(
            registry=registry, policy=policy, audit=audit, context=context, ask=ask
        )

    prompt = system_prompt()
    if not await store.has_sessions():
        prompt += ONBOARDING_SUFFIX
    manager = SessionManager(
        provider=provider,
        tools_factory=tools_factory,
        store=store,
        default_model=config.default_model,
        system_prompt=prompt,
        max_concurrent=config.max_concurrent_sessions,
        budget=budget,
        downgrade_model=config.models.get("downgrade"),
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
    register_selfedit_tools(
        registry, SelfEditPipeline(Path.cwd(), audit, restart_daemon)
    )
    dispatcher.set_commands(CommandSet(manager, monitor_service, cron_service))

    socket_adapter = SocketAdapter(config.socket_path, dispatcher.handle)
    dispatcher.register(socket_adapter)

    web_adapter = WebAdapter()
    dispatcher.register(web_adapter)
    web_server: WebServer | None = None
    if config.web_password:
        web_app = build_web_app(
            Auth(config.web_password), web_adapter, dispatcher.handle, monitor_service
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
    )
