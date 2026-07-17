"""Web auth: restart-stable session token, rotation, persisted secret (#188)."""

from pathlib import Path

from starlette.datastructures import Headers
from starlette.requests import Request

from chief.web.auth import COOKIE_NAME, Auth, load_or_create_secret


def request_with_cookie(value: str) -> Request:
    headers = Headers({"cookie": f"{COOKIE_NAME}={value}"})
    return Request({"type": "http", "headers": headers.raw})


def test_token_is_stable_across_instances_with_same_secret() -> None:
    # Two Auth instances = two daemon boots; same secret + password → same
    # token, so an already-logged-in browser stays authed after a restart.
    first = Auth("hunter2", "server-secret")
    second = Auth("hunter2", "server-secret")
    assert first.cookie_value() == second.cookie_value()
    assert second.is_authed(request_with_cookie(first.cookie_value()))


def test_token_changes_when_password_changes() -> None:
    old = Auth("hunter2", "server-secret")
    new = Auth("hunter3", "server-secret")
    assert old.cookie_value() != new.cookie_value()
    assert not new.is_authed(request_with_cookie(old.cookie_value()))


def test_rotating_secret_logs_everyone_out() -> None:
    old = Auth("hunter2", "secret-a")
    rotated = Auth("hunter2", "secret-b")
    assert not rotated.is_authed(request_with_cookie(old.cookie_value()))


def test_no_password_never_authes() -> None:
    auth = Auth("", "server-secret")
    assert not auth.enabled
    assert auth.cookie_value() == ""
    assert not auth.is_authed(request_with_cookie(""))


def test_load_or_create_secret_persists_and_reuses(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "web_session_secret"
    created = load_or_create_secret(path)
    assert path.read_text() == created
    assert load_or_create_secret(path) == created
    assert path.stat().st_mode & 0o777 == 0o600
