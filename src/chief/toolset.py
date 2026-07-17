"""Assemble chief's default native toolset onto a ToolRegistry.

One place wires every in-process tool the agent gets at boot: the
self-management tools (``session``/``monitor``/``schedule``/``self_edit``), the
read tools (``read_file``/``grep``), skills, package install, and subagent
spawn. Channel- and capability-specific tools arrive later as package-registered
tools; this is only the always-on core set.
"""

from pathlib import Path

from chief.agent.manager import SessionManager
from chief.agent.session_tools import register_session_tools
from chief.agent.tools import ToolRegistry
from chief.budget import Budget
from chief.cron.service import CronService
from chief.cron.tools import register_cron_tools
from chief.filetools import register_file_tools
from chief.monitors.service import MonitorService
from chief.monitors.tools import register_monitor_tools
from chief.packages import PackageLibrary
from chief.provider.base import Provider
from chief.selfedit.pipeline import SelfEditPipeline
from chief.selfedit.tools import register_install_tool, register_selfedit_tools
from chief.skills import SkillLibrary, register_skill_tools
from chief.subagents import AgentRegistry, register_spawn_tool


def register_native_tools(
    registry: ToolRegistry,
    *,
    manager: SessionManager,
    monitor_service: MonitorService,
    cron_service: CronService,
    selfedit_pipeline: SelfEditPipeline,
    skills: SkillLibrary,
    package_library: PackageLibrary,
    provider: Provider,
    agents_dir: Path,
    default_model: str,
    budget: Budget,
    root: Path,
) -> None:
    """Register the always-on native tool set onto ``registry`` at boot.

    ``root`` is the repo root the read/self-edit tools operate over;
    ``agents_dir`` seeds the subagent registry for ``spawn_agent``.
    """
    register_session_tools(registry, manager)
    register_monitor_tools(registry, monitor_service)
    register_cron_tools(registry, cron_service)
    register_selfedit_tools(registry, selfedit_pipeline)
    register_file_tools(registry, root)
    register_skill_tools(registry, skills)
    register_install_tool(registry, selfedit_pipeline, package_library)
    register_spawn_tool(
        registry, AgentRegistry(agents_dir), provider, default_model, budget
    )
