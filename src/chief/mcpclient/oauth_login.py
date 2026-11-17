"""The owner-driven half of a login: start one, finish it from the address the
owner's browser ended up on, and end it (logout)."""

import asyncio
import logging
import secrets
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from chief.mcpclient.oauth_flow import callback_params
from chief.mcpclient.oauth_types import (
    LoginAttempt,
    LoginError,
    LoginResult,
    ServerAuth,
    Surface,
)
from chief.mcpclient.token_store import FileTokenStorage

logger = logging.getLogger(__name__)

LOGIN_WAIT_SECONDS = 60.0  # how long a /login or a paste waits on the far end
LINK_SENT = "login link sent to the owner"
REPLACED = "replaced by a newer login request"
_UNAVAILABLE = {
    "connected": "{} is already connected",
    "connecting": "{} is still connecting; try again in a moment",
    "disabled": "{} failed to start; check the daemon log, then restart",
}


async def start(record: ServerAuth, surface: Surface) -> str:
    """Arm an interactive attempt for ``record`` (replacing an open one) and
    wait for its link to reach ``surface`` — or for the attempt to end first."""
    name = record.config.name
    if record.state != "needs_auth":
        return _UNAVAILABLE[record.state].format(name)
    if record.attempt is not None:
        record.attempt.finish(LoginResult(False, f"login failed: {REPLACED}"))
    record.reject_flow(REPLACED)
    loop = asyncio.get_running_loop()
    attempt = LoginAttempt(name, surface, loop.create_future(), loop.create_future())
    record.attempt = attempt
    record.wake.set()
    outcomes: set[asyncio.Future[Any]] = {attempt.link, attempt.done}
    await asyncio.wait(
        outcomes, timeout=LOGIN_WAIT_SECONDS, return_when=asyncio.FIRST_COMPLETED
    )
    if attempt.done.done():
        return attempt.done.result().message
    if attempt.link.done():
        return LINK_SENT
    record.attempt = None  # a link that turns up late goes nowhere
    return f"error: timed out reaching {name}'s login page; try /login again"


async def complete(
    records: Iterable[ServerAuth],
    callback_url: str,
    server: str | None,
    clock: Callable[[], float],
    ttl: float,
) -> LoginResult:
    """Finish the login the pasted ``callback_url`` answers, if any is open.

    The link's ``state`` is the only thing that ties an address to a login, so
    it is matched in constant time against the open links and nothing else is
    believed: an unknown state completes nothing."""
    params = callback_params(callback_url)
    unknown = LoginResult(
        False, f"flow expired or not recognized — /login {server or '<server>'} again"
    )
    record = _match(records, params.get("state", ""), server)
    if record is None or record.flow is None or record.attempt is None:
        return unknown
    flow, attempt = record.flow, record.attempt
    record.flow = None  # one answer per link
    if clock() - flow.created_at > ttl:
        flow.future.set_exception(LoginError("the login link expired"))
        return unknown
    if "error" in params:
        flow.future.set_exception(LoginError(f"login denied ({params['error']})"))
    elif not params.get("code"):
        flow.future.set_exception(LoginError("the address has no authorization code"))
    else:
        flow.future.set_result((params["code"], params["state"]))
    try:
        return await asyncio.wait_for(asyncio.shield(attempt.done), LOGIN_WAIT_SECONDS)
    except TimeoutError:
        return LoginResult(False, f"{flow.server} is taking too long to connect")


async def logout(record: ServerAuth, storage: FileTokenStorage) -> LoginResult:
    """Forget ``record``'s login: revoke it where the server allows, delete the
    stored tokens, and end the live connection so it cannot outlive them."""
    name = record.config.name
    if record.state == "connecting":
        return LoginResult(False, _UNAVAILABLE["connecting"].format(name))
    await _revoke(storage)
    storage.clear()
    record.reject_flow("logged out")
    if record.state == "connected":
        record.state = "needs_auth"  # calls stop at once; the supervisor follows
        record.dropped.set()
    return LoginResult(True, f"{name} logged out")


def _match(
    records: Iterable[ServerAuth], state: str, server: str | None
) -> ServerAuth | None:
    if not state:
        return None
    for record in records:
        flow = record.flow
        if flow is None or server not in (None, record.config.name):
            continue
        if secrets.compare_digest(flow.state.encode(), state.encode()):
            return record
    return None


async def _revoke(storage: FileTokenStorage) -> None:
    """Tell the authorization server to drop the tokens (RFC 7009), if it has a
    revocation endpoint; best effort, a failure never blocks a logout."""
    endpoint, tokens = storage.revocation_endpoint(), await storage.get_tokens()
    if not endpoint or tokens is None:
        return
    info = await storage.get_client_info()
    hint = "refresh_token" if tokens.refresh_token else "access_token"
    data = {
        "token": tokens.refresh_token or tokens.access_token,
        "token_type_hint": hint,
        "client_id": (info.client_id if info else None) or "",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(endpoint, data=data)
    except httpx.HTTPError as exc:
        logger.warning("could not revoke at %s: %s", endpoint, exc)
        return
    if response.is_error:
        logger.warning("revoking at %s answered %s", endpoint, response.status_code)
