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


def connected(record: ServerAuth, count: int, attempt: LoginAttempt | None) -> bool:
    """End the logins that got ``record``'s server up; False when there were none.

    ``attempt`` is the ``/login`` the connection was made for, which is not always
    ``record.attempt``: a ``start`` that gave up waiting cleared that, and a newer
    ``/login`` replaced it. Both end here — the first so that a later network
    blip is not read as its login breaking, the second so that its owner, who is
    still waiting, gets the answer."""
    ending = [a for a in (attempt, record.attempt) if a is not None and not a.connected]
    record.attempt = None
    plural = "tool" if count == 1 else "tools"
    result = LoginResult(True, f"{record.config.name} connected ({count} {plural})")
    for ended in ending:
        ended.connected = True
        ended.finish(result)
    return bool(ending)


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
