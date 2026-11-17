"""The supervisor's side of a ``/login``: how a parked server picks up the attempt
that wakes it, and how that attempt ends once its connection comes up or breaks.
(The owner's side — start, paste back, logout — is ``oauth_login``.)"""

import logging

from chief.mcpclient.oauth_types import (
    LoginAttempt,
    LoginError,
    LoginResult,
    ServerAuth,
    leaf,
)

logger = logging.getLogger(__name__)


async def next_attempt(record: ServerAuth) -> LoginAttempt:
    """Wait until the owner asks to log in to ``record``'s server."""
    while True:
        await record.wake.wait()
        record.wake.clear()
        if record.attempt is not None and not record.attempt.done.done():
            return record.attempt


def connected(record: ServerAuth, count: int) -> bool:
    """End the login that got ``record``'s server up; False when there was none."""
    attempt, record.attempt = record.attempt, None
    if attempt is None or attempt.connected:
        return False
    attempt.connected = True
    plural = "tool" if count == 1 else "tools"
    name = record.config.name
    attempt.finish(LoginResult(True, f"{name} connected ({count} {plural})"))
    return True


def failed(record: ServerAuth, attempt: LoginAttempt, exc: BaseException) -> str:
    """End ``attempt`` with the reason it broke; returns the error's type name,
    which is what the audit trail records."""
    error = leaf(exc)
    reason = str(error) if isinstance(error, LoginError) else repr(error)
    logger.warning("mcp login for %s failed: %s", attempt.server, reason)
    attempt.finish(LoginResult(False, f"login failed: {reason}"))
    if record.attempt is attempt:
        record.attempt = None
    return type(error).__name__
