"""Telling the owner a server needs login: once per wall, across restarts."""

import logging
from pathlib import Path

from chief.gate import load_approved as load_names
from chief.gate import save_approved as save_names
from chief.mcpclient.oauth_types import Send, Surface

logger = logging.getLogger(__name__)


class Notices:
    """The "needs login" notices already sent, remembered in a small json file.

    A server that stays logged out must not text the owner on every boot, so a
    notice is due once; a successful login clears it, because the *next* wall is
    news again. A logout the owner asked for is marked sent: they know."""

    def __init__(self, send: Send, target: Surface, path: Path) -> None:
        self._send = send
        self._target = target
        self._path = path
        try:
            self._sent = load_names(path)
        except (OSError, ValueError):
            logger.warning("%s is unreadable; treating no one as notified", path)
            self._sent = set()

    def due(self, server: str) -> bool:
        return server not in self._sent

    async def send(self, server: str) -> None:
        """Best effort: a dead channel is logged, and the notice stays due."""
        text = (
            f'{server.capitalize()} needs login. '
            f'Say "connect {server}" or /login {server}.'
        )
        try:
            await self._send(self._target, text)
        except Exception:
            logger.exception("could not tell the owner %s needs login", server)
            return
        self.mark(server)

    def mark(self, server: str) -> None:
        self._sent.add(server)
        save_names(self._sent, self._path)

    def clear(self, server: str) -> None:
        if server in self._sent:
            self._sent.discard(server)
            save_names(self._sent, self._path)
