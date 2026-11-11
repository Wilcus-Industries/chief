"""Application wiring: build the whole daemon from a Config.

``build_app`` reads as a table of contents — each phase is a helper called in
order. Persistence, gate, MCP, and adapters live in ``chief.wiring``; the
default toolset in ``chief.tools.native``; hook assembly in ``chief.hooks.boot``;
the agent-core closure trap stays here.
Tests boot exactly this wiring with only the provider swapped for the
deterministic fake (the one scripted fake CI allows, PRD #183).
"""

import logging
from functools import partial
from pathlib import Path

from chief.adapters.imessage_send import owner_send_guard
from chief.agent.manager import SessionManager
from chief.agent.prompt import boot_prompt
from chief.budget import Budget
from chief.bus import EventBus
from chief.commands import CommandSet
from chief.config import Config
from chief.cron.service import CronService
from chief.cron.timing import parse_quiet_hours
from chief.cron.updates import ensure_update_schedule, primary_target
from chief.daemon import App
from chief.dispatch import Dispatcher
from chief.gate import GatedTools
from chief.gate_surface import approval_asker, card_pointer, channel_announcer
from chief.hooks.boot import build_hooks
from chief.hooks.posttool import tool_screener
from chief.hub import ObserverHub
from chief.monitors.service import MonitorService
from chief.peers.wiring import build_peers
from chief.persistence.db import SessionFactory
from chief.persistence.store import MessageStore
from chief.policy import owner_audience
from chief.provider.base import Provider
from chief.selfedit.inflight import InflightLog
from chief.selfedit.pipeline import SelfEditPipeline
from chief.selfedit.recovery import RestartController
from chief.skills import SkillLibrary
from chief.strangers import StrangerLog
from chief.subagents import SubagentScope
from chief.tools import ToolContext, ToolDispatcher, ToolRegistry
from chief.tools.native import register_native_tools
from chief.tools.shell.prompt import shell_prompt_line
from chief.tools.shell.service import ShellGuard, ShellService, guarded_runner
from chief.wiring import (
    Core,
    Gate,
    build_adapters,
    build_compactor,
    build_gate,
    build_mcp,
    build_persistence,
    build_provider,
)

logger = logging.getLogger(__name__)

__all__ = ["App", "build_app"]


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
    hooks, classifier, screen = build_hooks(config, provider, budget, gate.audit)

    audience = partial(owner_audience, owner_handles=config.imessage_owner_handles)

    def tools_factory(
        thread_key: str, channel: str, subagent: str | None = None
    ) -> ToolDispatcher:
        # A subagent's calls are gated on its parent's thread, unannounced.
        context = ToolContext(thread_key, channel, subagent)
        announce = channel_announcer(dispatcher, audience)
        return GatedTools(
            registry=SubagentScope(registry) if subagent else registry,
            policy=gate.policy, audit=gate.audit,
            context=context, ask=ask, on_always=gate.allow_always,
            announce=announce if config.gate_announce and not subagent else None,
        )

    restart = RestartController(repo_root=Path.cwd())
    inflight = InflightLog(config.db_path.parent)
    manager = SessionManager(
        provider=provider,
        tools_factory=tools_factory,
        store=store,
        default_model=config.default_model,
        system_prompt=await boot_prompt(
            store, skills.prompt_lines() + shell_prompt_line()
        ),
        max_concurrent=config.max_concurrent_sessions,
        budget=budget,
        downgrade_model=config.models.get("downgrade"),
        compactor=build_compactor(config, provider),
        restart_gate=restart,
        hooks=hooks,
        hooks_timeout_seconds=config.hooks_timeout_seconds,
    )
    dispatcher = Dispatcher(
        manager, bus=bus, hub=hub, approvals=gate.approvals,
        strangers=StrangerLog(factory), restart=restart,
        channel_defaults=config.stream_channel_defaults, audience=audience,
        inflight=inflight,
    )
    pointer = card_pointer(dispatcher, primary_target(config))
    ask = approval_asker(dispatcher, gate.approvals, audience, pointer)
    monitors = MonitorService(factory, bus, dispatcher.handle, classifier, screen,
                              config.imessage_owner_handles)
    cron = CronService(
        factory, dispatcher.handle, parse_quiet_hours(config.quiet_hours),
        run_command=guarded_runner(shell_service, shell_guards),
    )
    # Subagent turns get the same post_tool seam (core injection screen first).
    post_tool = tool_screener(hooks, config.hooks_timeout_seconds, logger)
    return Core(budget, bus, hub, registry, restart, manager, dispatcher,
                monitors, cron, post_tool, ask, tools_factory, inflight)


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

    peers = build_peers(core, config, store, gate.audit, provider)
    selfedit_pipeline = SelfEditPipeline(Path.cwd(), gate.audit, core.restart.request)
    # Accepted by `/model` / `switch_model` — see chief.provider.model_names.
    model_aliases = frozenset(config.provider_aliases)
    # A forward to the owner targets this row by its `(owner, primary)` tag,
    # and send_msg refuses an unknown thread — so it exists from boot.
    primary = primary_target(config)
    await store.ensure_session(primary[1], primary[0])
    register_native_tools(
        core.registry,
        manager=core.manager,
        monitor_service=core.monitors,
        cron_service=core.cron,
        selfedit_pipeline=selfedit_pipeline, skills=skills,
        shell_service=shell_service, agents_dir=config.agents_dir,
        root=Path.cwd(), shell_guards=shell_guards, model_aliases=model_aliases,
        ask=core.ask, router=peers.router, pool=peers.pool,
        owner_handles=config.imessage_owner_handles, primary=primary,
    )
    await ensure_update_schedule(core.cron, config)
    commands = CommandSet(
        core.manager, core.monitors, core.cron, store, skills=skills,
        model_aliases=model_aliases, pool=peers.pool,
    )
    core.dispatcher.set_commands(commands)
    mcp_manager, mcp_configs = build_mcp(config, core.registry)
    adapters = build_adapters(config, core, store, commands, gate.approvals)

    return App(
        config=config, engine=engine, store=store, dispatcher=core.dispatcher,
        socket_adapter=adapters.socket, monitor_service=core.monitors,
        cron_service=core.cron, bus=core.bus, hub=core.hub,
        web_adapter=adapters.web_adapter, web_server=adapters.web_server,
        mcp_manager=mcp_manager, mcp_configs=mcp_configs,
        imessage_adapter=adapters.imessage, shell_service=shell_service,
        peers=peers, inflight=core.inflight,
    )
