"""Owner-only password auth for the web UI (#153).

One credential: a password hashed at rest (scrypt) in the secrets dir, alongside the
platform tokens it fence-shares (0600, never committed). Sessions are opaque random
tokens delivered as a persistent cookie; only their SHA-256 digests are stored, in a
sessions file next to the hash, so a disk read never yields a usable cookie. Changing
the password rewrites the hash AND clears the token store — that is the revocation
story ("sessions revocable via password change").

Deliberately pluggable: :class:`WebAuth` is the only authenticator the app knows, and
it is injected (``WebDeps.auth``) rather than imported by the routes — the SaaS future
fronts the same app with its own identity layer by swapping this object, nothing else.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

#: One-file-per-secret names, following the secrets-dir convention
#: (``secrets/README.md``): the scrypt'd credential and the session-digest store.
PASSWORD_FILE: Final[str] = "web_password"
SESSIONS_FILE: Final[str] = "web_sessions.json"

#: The persistent session cookie ("login once per browser").
SESSION_COOKIE: Final[str] = "chief_session"

#: Cookie lifetime: ~half a year. The cookie outliving the browser session is the
#: autologin; the server-side digest store is what actually decides validity.
SESSION_MAX_AGE: Final[int] = 180 * 24 * 3600

#: scrypt parameters — the interactive-login preset (RFC 7914 suggests N=2**14 for
#: interactive use); ~50 ms per verify, plenty against an offline copy of the file.
_SCRYPT_N: Final[int] = 2**14
_SCRYPT_R: Final[int] = 8
_SCRYPT_P: Final[int] = 1
_SALT_BYTES: Final[int] = 16
_KEY_BYTES: Final[int] = 32

#: Owner read/write only — the same fence class as every other secrets-dir file.
_SECRET_MODE: Final[int] = 0o600


def hash_password(password: str) -> str:
    """Hash ``password`` for at-rest storage: ``scrypt$N$r$p$salt_b64$key_b64``."""
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_BYTES,
    )
    encoded_salt = base64.b64encode(salt).decode("ascii")
    encoded_key = base64.b64encode(key).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${encoded_salt}${encoded_key}"


def verify_password(password: str, stored: str) -> bool:
    """True iff ``password`` matches a :func:`hash_password`-formatted ``stored``.

    A malformed ``stored`` value verifies false rather than raising: the file is
    owner-authored state, and a corrupt credential must fail closed (no login), not
    crash the request.
    """
    try:
        scheme, n, r, p, encoded_salt, encoded_key = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(encoded_salt)
        expected = base64.b64decode(encoded_key)
        key = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key, expected)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class WebAuth:
    """The credential + session store over one directory (the secrets dir).

    All I/O is small synchronous file work (two tiny owner-only files); callers run
    it inline on the event loop, which a single-owner surface tolerates — the scrypt
    verify (~50 ms) happens once per login, not per request.
    """

    def __init__(self, auth_dir: Path | str) -> None:
        self._dir = Path(auth_dir)
        self._password_file = self._dir / PASSWORD_FILE
        self._sessions_file = self._dir / SESSIONS_FILE

    @property
    def password_set(self) -> bool:
        """True once a credential exists — gates /setup vs /login."""
        return self._password_file.is_file()

    def set_password(self, password: str) -> None:
        """Write the hashed credential and revoke every existing session."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._write_secret(self._password_file, hash_password(password))
        self.revoke_all()

    def verify(self, password: str) -> bool:
        """True iff ``password`` matches the stored credential (false when unset)."""
        if not self.password_set:
            return False
        return verify_password(password, self._password_file.read_text())

    def issue_session(self) -> str:
        """Mint a session token; persist only its digest. Returns the cookie value."""
        token = secrets.token_urlsafe(32)
        digests = self._load_digests()
        digests.append(_digest(token))
        self._store_digests(digests)
        return token

    def session_valid(self, token: str | None) -> bool:
        """True iff ``token`` names a live (not-revoked) session."""
        if not token:
            return False
        wanted = _digest(token)
        # compare_digest over every entry: no early-exit timing tell on which (if
        # any) stored digest matched.
        found = False
        for stored in self._load_digests():
            if hmac.compare_digest(stored, wanted):
                found = True
        return found

    def revoke(self, token: str) -> None:
        """Drop one session (logout). Unknown tokens are a no-op."""
        wanted = _digest(token)
        digests = [d for d in self._load_digests() if d != wanted]
        self._store_digests(digests)

    def revoke_all(self) -> None:
        """Clear the whole token store — every browser must log in again."""
        self._store_digests([])

    def _load_digests(self) -> list[str]:
        if not self._sessions_file.is_file():
            return []
        try:
            loaded = json.loads(self._sessions_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # Fail closed: an unreadable store means no session is valid, but the
            # owner can always log in again (which rewrites it).
            logger.warning("web session store unreadable, treating as empty: %s", exc)
            return []
        tokens = loaded.get("tokens") if isinstance(loaded, dict) else None
        if not isinstance(tokens, list):
            return []
        return [t for t in tokens if isinstance(t, str)]

    def _store_digests(self, digests: list[str]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._write_secret(self._sessions_file, json.dumps({"tokens": digests}))

    @staticmethod
    def _write_secret(path: Path, content: str) -> None:
        # touch-then-chmod-then-write: never leave secret bytes in a window where
        # the file carries the default (group/other-readable) mode.
        path.touch(mode=_SECRET_MODE, exist_ok=True)
        path.chmod(_SECRET_MODE)
        path.write_text(content)
