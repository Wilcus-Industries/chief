"""Owner-password auth for the web UI.

One password, one bearer cookie. The cookie is an HMAC of the password under a
server secret that is persisted across restarts, so a normal daemon restart
does NOT log the owner out (issue #188). The token rotates automatically when
the password changes, and rotating the persisted secret ("log out everywhere")
invalidates every session. Fail closed: no password, nobody logs in.
"""

import hashlib
import hmac
import secrets
from pathlib import Path

from starlette.requests import Request

COOKIE_NAME = "chief_session"
SECRET_PATH = Path("secrets/web_session_secret")


def load_or_create_secret(path: Path = SECRET_PATH) -> str:
    """Read the persisted session secret, minting and storing one if absent."""
    if path.exists():
        return path.read_text().strip()
    secret = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret)
    path.chmod(0o600)
    return secret


class Auth:
    """Checks the owner password and a restart-stable session cookie."""

    def __init__(self, password: str, secret: str | None = None) -> None:
        self._password = password
        # No persisted secret (e.g. in tests) → an ephemeral per-boot one.
        self._secret = secret if secret is not None else secrets.token_urlsafe(32)
        self._token = self._derive() if password else ""

    def _derive(self) -> str:
        return hmac.new(
            self._secret.encode(), self._password.encode(), hashlib.sha256
        ).hexdigest()

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
        return (
            self.enabled
            and bool(cookie)
            and hmac.compare_digest(cookie, self._token)
        )
