"""Application wiring: build the whole daemon from a Config.

``build_app`` reads as a table of contents — each phase is a helper called in
order. Persistence, gate, MCP, and adapters live in ``chief.wiring``; the
default toolset in ``chief.toolset``; the agent-core closure trap stays here.
Tests boot exactly this wiring with only the provider swapped for the
deterministic fake (the one scripted fake CI allows, PRD #183).
"""

import logging
from pathlib import Path

from chief.agent.compaction import Compactor
from chief.agent.manager import SessionManager
from chief.agent.prompt import ONBOARDING_SUFFIX, system_prompt
from chief.agent.tools import ToolContext, ToolDispatcher, ToolRegistry
from chief.approvals import Approval
from chief.budget import Budget
from chief.bus import EventBus
from chief.classifiers import Classifier, ClassifierRegistry
from chief.commands import CommandSet
from chief.config import Config
from chief.cron.service import CronService
from chief.cron.timing import parse_quiet_hours
from chief.daemon import App
from chief.dispatch import Dispatcher
from chief.gate import GatedTools
from chief.monitors.service import MonitorService
from chief.persistence.db import SessionFactory
from chief.persistence.store import MessageStore
from chief.provider.base import Provider
from chief.selfedit.pipeline import SelfEditPipeline
from chief.selfedit.recovery import RestartController
from chief.shelltool import ShellService, shell_prompt_line
from chief.skills import SkillLibrary
from chief.strangers import StrangerLog
from chief.toolset import register_native_tools
from chief.wiring import (
    Core,
    Gate,
    build_adapters,
    build_gate,
    build_mcp,
    build_persistence,
    build_provider,
)

logger = logging.getLogger(__name__)

__all__ = ["App", "build_app"]


async def _build_prompt(store: MessageStore, skills: SkillLibrary) -> str:
    prompt = system_prompt() + skills.prompt_lines() + shell_prompt_line()
    if not await store.has_sessions():
        prompt += ONBOARDING_SUFFIX
    return prompt


async def _build_agent_core(
    config: Config,
    provider: Provider,
    store: MessageStore,
    factory: SessionFactory,
    gate: Gate,
    skills: SkillLibrary,
) -> Core:
    """Budget, bus, registry, the manager/dispatcher/tools_factory trio, and the
    monitor/cron services. The trio stays in one scope so ``tools_factory``'s
    late binding of ``dispatcher`` (created below it) resolves at call time —
    splitting them would break the closure cycle."""
    budget = Budget(factory, config.budget_cap_usd, config.budget_warn_ratio)
    bus = EventBus()
    registry = ToolRegistry()

    def tools_factory(thread_key: str, channel: str) -> ToolDispatcher:
        context = ToolContext(thread_key=thread_key, channel=channel)

        async def ask(ctx: ToolContext, question: str) -> Approval:
            send = dispatcher.adapter(ctx.channel).send
            return await gate.approvals.ask(
                ctx.thread_key, question, lambda q: send(ctx.thread_key, q)
            )

        return GatedTools(
            registry=registry, policy=gate.policy, audit=gate.audit,
            context=context, ask=ask, on_always=gate.allow_always,
        )

    restart = RestartController()
    manager = SessionManager(
        provider=provider,
        tools_factory=tools_factory,
        store=store,
        default_model=config.default_model,
        system_prompt=await _build_prompt(store, skills),
        max_concurrent=config.max_concurrent_sessions,
        budget=budget,
        downgrade_model=config.models.get("downgrade"),
        compactor=Compactor(provider, config.default_model),
        restart_gate=restart,
    )
    dispatcher = Dispatcher(
        manager, bus=bus, approvals=gate.approvals,
        strangers=StrangerLog(factory), restart=restart,
    )
    classifier = Classifier(
        provider,
        ClassifierRegistry(config.classifiers_dir),
        config.models.get("default_classifier", config.default_model),
    )
    monitors = MonitorService(factory, bus, dispatcher.handle, classifier)
    cron = CronService(
        factory, dispatcher.handle, parse_quiet_hours(config.quiet_hours)
    )
    return Core(budget, bus, registry, restart, manager, dispatcher, monitors, cron)


async def build_app(config: Config, provider: Provider | None = None) -> App:
    """Assemble every core service; ``provider`` overrides the built one (tests)."""
    provider = provider or build_provider(config)
    engine, factory, store = await build_persistence(config)
    gate = build_gate(config)
    skills = SkillLibrary(config.skills_dir)
    core = await _build_agent_core(config, provider, store, factory, gate, skills)

    selfedit_pipeline = SelfEditPipeline(Path.cwd(), gate.audit, core.restart.request)
    shell_service = ShellService(
        workspace_dir=str(Path.cwd()),
        timeout_seconds=config.shell_timeout_seconds,
        output_limit=config.shell_output_limit,
    )
    register_native_tools(
        core.registry,
        manager=core.manager,
        monitor_service=core.monitors,
        cron_service=core.cron,
        selfedit_pipeline=selfedit_pipeline,
        skills=skills,
        shell_service=shell_service,
        provider=provider,
        agents_dir=config.agents_dir,
        default_model=config.default_model,
        budget=core.budget,
        root=Path.cwd(),
    )
    commands = CommandSet(core.manager, core.monitors, core.cron, store, skills=skills)
    core.dispatcher.set_commands(commands)
    mcp_manager, mcp_configs = build_mcp(config, core.registry)
    adapters = build_adapters(config, core, store, commands)

    return App(
        config=config,
        engine=engine,
        store=store,
        dispatcher=core.dispatcher,
        socket_adapter=adapters.socket,
        monitor_service=core.monitors,
        cron_service=core.cron,
        bus=core.bus,
        web_adapter=adapters.web_adapter,
        web_server=adapters.web_server,
        mcp_manager=mcp_manager,
        mcp_configs=mcp_configs,
        imessage_adapter=adapters.imessage,
        shell_service=shell_service,
    )
