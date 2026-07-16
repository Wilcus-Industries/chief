"""Owner-password auth for the web UI.

One password, one bearer cookie. The cookie value is random per boot, so a
restart invalidates old sessions; no signing machinery needed.
"""

import hmac
import secrets

from starlette.requests import Request

COOKIE_NAME = "chief_session"


class Auth:
    """Checks the owner password and the per-boot session cookie."""

    def __init__(self, password: str) -> None:
        self._password = password
        self._token = secrets.token_urlsafe(32)

    @property
    def enabled(self) -> bool:
        """Without a configured password nobody can log in (fail closed)."""
        return bool(self._password)

    def check_password(self, attempt: str) -> bool:
        return self.enabled and hmac.compare_digest(attempt, self._password)

    def cookie_value(self) -> str:
        return self._token

    def is_authed(self, request: Request) -> bool:
        cookie = request.cookies.get(COOKIE_NAME, "")
        return bool(cookie) and hmac.compare_digest(cookie, self._token)
