"""Boot phase for messaging: the router and the subagent pool, wired into the
manager (orphan cleanup) and the restart controller (restart notices) — kept
out of ``chief.app`` so that file stays a short table of contents."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from chief.agent.subagent_notices import write_notices
from chief.agent.subagent_pool import SubagentPool
from chief.agent.subagent_run import RunDeps
from chief.audit import AuditLog
from chief.config import Config
from chief.peers.loopguard import PairCounter
from chief.peers.router import PeerRouter
from chief.persistence.store import MessageStore
from chief.provider.base import Provider
from chief.wiring import Core


@dataclass
class Peers:
    """The messaging services ``build_app`` threads into tools and the App.

    ``pre_restart`` records the live subagents so each parent hears, after the
    reboot, that its subagent died (registered on the restart controller)."""

    router: PeerRouter
    pool: SubagentPool
    pre_restart: Callable[[], None]

    async def aclose(self) -> None:
        """Silently cancel subagents, then in-flight deliveries (shutdown)."""
        await self.pool.aclose()
        await self.router.aclose()


def build_peers(
    core: Core, config: Config, store: MessageStore, audit: AuditLog,
    provider: Provider,
) -> Peers:
    """The router (waking idle receivers through the real dispatcher) and the
    pool (each run gated through the parent's tools factory)."""
    if core.tools_factory is None:
        raise ValueError("build_peers needs the core's tools_factory")
    counter = PairCounter(config.peers_loop_warn_count, config.peers_loop_window_s)
    router = PeerRouter(core.manager, store, audit, core.dispatcher.handle, counter)
    deps = RunDeps(
        provider=provider, default_model=config.default_model,
        slots=asyncio.Semaphore(config.subagents_max_concurrent),
        budget=core.budget, post_tool=core.post_tool,
    )
    pool = SubagentPool(deps, core.tools_factory, router.deliver, audit,
                        float(config.subagents_idle_timeout_s))
    router.pool = pool
    core.manager.set_on_wipe(pool.cancel_children)
    data_dir = config.db_path.parent

    def pre_restart() -> None:
        write_notices(data_dir, pool.snapshot_for_restart())

    core.restart.add_pre_restart(pre_restart)
    return Peers(router, pool, pre_restart)
