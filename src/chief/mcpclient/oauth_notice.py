"""Telling the owner a server needs login: once per wall, across restarts."""

import asyncio
import logging
from pathlib import Path

# The gate's json-list-of-names persistence, reused for the same shape of data.
from chief.gate import load_approved as load_names
from chief.gate import save_approved as save_names
from chief.mcpclient.oauth_types import Send, Surface

logger = logging.getLogger(__name__)

#: How long a notice may take to send. A channel that hangs must not keep the
#: supervisor from listening for the owner's /login.
NOTICE_TIMEOUT_SECONDS = 10.0


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
        """Best effort: a dead or hung channel is logged, and the notice stays due."""
        text = (
            f'{server.capitalize()} needs login. '
            f'Say "connect {server}" or /login {server}.'
        )
        try:
            await asyncio.wait_for(
                self._send(self._target, text), NOTICE_TIMEOUT_SECONDS
            )
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
