"""Application wiring: build the whole daemon from a Config.

``build_app`` reads as a table of contents — each phase is a helper called in
order. Persistence, gate, MCP, and adapters live in ``chief.wiring``; the
default toolset in ``chief.tools.native``; hook assembly in ``chief.hooks.boot``;
the agent-core closure trap stays here.
Tests boot exactly this wiring with only the provider swapped for the
deterministic fake (the one scripted fake CI allows, PRD #183).
"""

import logging
from pathlib import Path

from chief.adapters.imessage_send import owner_send_guard
from chief.agent.compaction import Compactor
from chief.agent.manager import SessionManager
from chief.agent.prompt import ONBOARDING_SUFFIX, system_prompt
from chief.agent.windows import WindowResolver
from chief.budget import Budget
from chief.bus import EventBus
from chief.commands import CommandSet
from chief.config import Config
from chief.cron.service import CronService
from chief.cron.timing import parse_quiet_hours
from chief.cron.updates import ensure_update_schedule
from chief.daemon import App
from chief.dispatch import Dispatcher
from chief.gate import GatedTools, approval_asker
from chief.hooks.boot import build_hooks
from chief.hub import ObserverHub
from chief.monitors.service import MonitorService
from chief.persistence.db import SessionFactory
from chief.persistence.store import MessageStore
from chief.provider.base import Provider
from chief.selfedit.pipeline import SelfEditPipeline
from chief.selfedit.recovery import RestartController
from chief.skills import SkillLibrary
from chief.strangers import StrangerLog
from chief.tools import ToolContext, ToolDispatcher, ToolRegistry
from chief.tools.native import register_native_tools
from chief.tools.shell.prompt import shell_prompt_line
from chief.tools.shell.service import ShellGuard, ShellService, guarded_runner
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
    config: Config, provider: Provider, store: MessageStore,
    factory: SessionFactory, gate: Gate, skills: SkillLibrary,
    shell_service: ShellService, shell_guards: tuple[ShellGuard, ...],
) -> Core:
    """Budget, bus, registry, the manager/dispatcher/tools_factory trio, and the
    monitor/cron services. The trio stays in one scope so ``tools_factory``'s
    late binding of ``dispatcher`` (created below it) resolves at call time —
    splitting them would break the closure cycle."""
    budget = Budget(factory, config.budget_cap_usd, config.budget_warn_ratio)
    bus = EventBus()
    hub = ObserverHub()
    registry = ToolRegistry()
    hooks, classifier = build_hooks(config, provider, budget)

    async def announce(ctx: ToolContext, text: str) -> None:
        if ctx.agent:  # name the subagent — the owner did not initiate this
            text = f"subagent '{ctx.agent}' {text}"
        await dispatcher.adapter(ctx.channel).send(ctx.thread_key, text)

    def gated(context: ToolContext, inner: ToolDispatcher) -> ToolDispatcher:
        return GatedTools(
            registry=inner, policy=gate.policy, audit=gate.audit,
            context=context, ask=approval_asker(dispatcher, gate.approvals),
            on_always=gate.allow_always,
            announce=announce if config.gate_announce else None,
        )

    def tools_factory(thread_key: str, channel: str) -> ToolDispatcher:
        return gated(ToolContext(thread_key=thread_key, channel=channel), registry)

    restart = RestartController(repo_root=Path.cwd())
    window_resolver = WindowResolver(
        windows=config.compaction_windows, base_url=config.provider_base_url,
        default_window=config.compaction_default_window,
        aliases=config.provider_aliases.keys(), api_key=config.openrouter_api_key,
    )
    manager = SessionManager(
        provider=provider,
        tools_factory=tools_factory,
        store=store,
        default_model=config.default_model,
        system_prompt=await _build_prompt(store, skills),
        max_concurrent=config.max_concurrent_sessions,
        budget=budget,
        downgrade_model=config.models.get("downgrade"),
        compactor=Compactor(
            provider, config.default_model, window_resolver,
            ratio=config.compaction_ratio, keep_recent=config.compaction_keep_recent,
        ),
        restart_gate=restart,
        hooks=hooks,
        hooks_timeout_seconds=config.hooks_timeout_seconds,
    )
    dispatcher = Dispatcher(
        manager, bus=bus, hub=hub, approvals=gate.approvals,
        strangers=StrangerLog(factory), restart=restart,
        channel_defaults=config.stream_channel_defaults,
    )
    monitors = MonitorService(factory, bus, dispatcher.handle, classifier)
    cron = CronService(
        factory, dispatcher.handle, parse_quiet_hours(config.quiet_hours),
        run_command=guarded_runner(shell_service, shell_guards),
    )
    return Core(
        budget, bus, hub, registry, restart, manager, dispatcher, monitors, cron,
        gated,
    )


async def build_app(config: Config, provider: Provider | None = None) -> App:
    """Assemble every core service; ``provider`` overrides the built one (tests)."""
    provider = provider or build_provider(config)
    engine, factory, store = await build_persistence(config)
    gate = build_gate(config)
    skills = SkillLibrary(config.skills_dir)
    # Built before the core: cron runs command schedules on these shells.
    shell_service = ShellService(
        workspace_dir=str(Path.cwd()),
        timeout_seconds=config.shell_timeout_seconds,
        output_limit=config.shell_output_limit,
    )
    # Echo-loop seatbelt on BOTH shell paths: the tool and cron's runner.
    shell_guards = (owner_send_guard(config.echo_guarded_handles),)
    core = await _build_agent_core(
        config, provider, store, factory, gate, skills, shell_service, shell_guards
    )

    selfedit_pipeline = SelfEditPipeline(Path.cwd(), gate.audit, core.restart.request)
    # Accepted by `/model` / `switch_model` — see chief.provider.model_names.
    model_aliases = frozenset(config.provider_aliases)
    register_native_tools(
        core.registry,
        manager=core.manager, monitor_service=core.monitors, cron_service=core.cron,
        selfedit_pipeline=selfedit_pipeline, skills=skills,
        shell_service=shell_service, provider=provider,
        agents_dir=config.agents_dir, default_model=config.default_model,
        budget=core.budget, root=Path.cwd(), shell_guards=shell_guards,
        model_aliases=model_aliases, gated=core.gated,
        ask=approval_asker(core.dispatcher, gate.approvals),
    )
    await ensure_update_schedule(core.cron, config)
    commands = CommandSet(
        core.manager, core.monitors, core.cron, store, skills=skills,
        model_aliases=model_aliases,
    )
    core.dispatcher.set_commands(commands)
    mcp_manager, mcp_configs = build_mcp(config, core.registry)
    adapters = build_adapters(config, core, store, commands, gate.approvals)

    return App(
        config=config,
        engine=engine,
        store=store,
        dispatcher=core.dispatcher,
        socket_adapter=adapters.socket,
        monitor_service=core.monitors,
        cron_service=core.cron,
        bus=core.bus,
        hub=core.hub,
        web_adapter=adapters.web_adapter,
        web_server=adapters.web_server,
        mcp_manager=mcp_manager,
        mcp_configs=mcp_configs,
        imessage_adapter=adapters.imessage,
        shell_service=shell_service,
    )
