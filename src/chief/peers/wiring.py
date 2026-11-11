"""Boot phase for messaging: the router (and, once built, the subagent pool),
kept out of ``chief.app`` so that file stays a short table of contents."""

from dataclasses import dataclass

from chief.audit import AuditLog
from chief.config import Config
from chief.peers.loopguard import PairCounter
from chief.peers.router import PeerRouter
from chief.persistence.store import MessageStore
from chief.wiring import Core


@dataclass
class Peers:
    """The messaging services ``build_app`` threads into tools and the App."""

    router: PeerRouter

    async def aclose(self) -> None:
        """Cancel in-flight deliveries (daemon shutdown)."""
        await self.router.aclose()


def build_peers(
    core: Core, config: Config, store: MessageStore, audit: AuditLog
) -> Peers:
    """The router, waking idle receivers through the real dispatcher."""
    counter = PairCounter(config.peers_loop_warn_count, config.peers_loop_window_s)
    router = PeerRouter(core.manager, store, audit, core.dispatcher.handle, counter)
    return Peers(router)
