"""Assemble chief's default native toolset onto a ToolRegistry.

One place wires every in-process tool the agent gets at boot: the
self-management tools (``session``/``monitor``/``schedule``), the file tools
(``read_file``/``grep``/``write_file``/``edit_file``), the host ``shell``, the
guarded ``restart``, skills, and subagent spawn. Package discovery is the
``chief-pkg`` CLI run through ``shell`` and install is document-driven, so neither
is its own native tool. Channel- and capability-specific tools arrive later as
package-registered tools; this is only the always-on core set.
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
from chief.provider.base import Provider
from chief.selfedit.pipeline import SelfEditPipeline
from chief.selfedit.tools import register_restart_tool
from chief.shelltool import ShellService, register_shell_tool
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
    shell_service: ShellService,
    provider: Provider,
    agents_dir: Path,
    default_model: str,
    budget: Budget,
    root: Path,
) -> None:
    """Register the always-on native tool set onto ``registry`` at boot.

    ``root`` is the repo root the file tools resolve relative paths against;
    ``agents_dir`` seeds the subagent registry for ``spawn_agent``;
    ``shell_service`` owns the host shells the ``shell`` tool drives.
    """
    register_session_tools(registry, manager)
    register_monitor_tools(registry, monitor_service)
    register_cron_tools(registry, cron_service)
    register_file_tools(registry, root)
    register_shell_tool(registry, shell_service)
    register_restart_tool(registry, selfedit_pipeline)
    register_skill_tools(registry, skills)
    register_spawn_tool(
        registry, AgentRegistry(agents_dir), provider, default_model, budget
    )
