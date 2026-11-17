"""FileTokenStorage: the per-server OAuth token + client-registration file."""

import json
import logging
import stat
import time
from pathlib import Path

import pytest
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)

from chief.mcpclient.token_store import FileTokenStorage

URL = "https://agent.example.com/mcp/trading"
REDIRECT = "http://127.0.0.1:8130/oauth/callback"
ISSUER = "https://api.example.com"  # not the MCP server's origin, like the real one
TOKEN_ENDPOINT = f"{ISSUER}/oauth2/token/"
REVOCATION_ENDPOINT = f"{ISSUER}/oauth2/revoke/"


def storage(
    tmp_path: Path, *, url: str = URL, redirect: str = REDIRECT, server: str = "rh"
) -> FileTokenStorage:
    return FileTokenStorage(tmp_path / "mcp-oauth", server, url, redirect)


def tokens(**fields: object) -> OAuthToken:
    base: dict[str, object] = {
        "access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600,
    }
    return OAuthToken.model_validate(base | fields)


def client_info() -> OAuthClientInformationFull:
    return OAuthClientInformationFull.model_validate(
        {
            "client_id": "client-1",
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
        }
    )


async def test_tokens_and_client_info_round_trip_through_a_fresh_instance(
    tmp_path: Path,
) -> None:
    first = storage(tmp_path)
    await first.set_client_info(client_info())
    await first.set_tokens(tokens())

    second = storage(tmp_path)
    assert await second.get_tokens() == tokens()
    assert await second.get_client_info() == client_info()


async def test_nothing_stored_reads_as_none(tmp_path: Path) -> None:
    empty = storage(tmp_path)
    assert await empty.get_tokens() is None
    assert await empty.get_client_info() is None


async def test_file_is_private_and_lands_atomically(tmp_path: Path) -> None:
    store = storage(tmp_path)
    await store.set_tokens(tokens())

    directory = tmp_path / "mcp-oauth"
    (path,) = list(directory.iterdir())  # no stray temp file left behind
    assert path.name == "rh.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


async def test_rewrite_keeps_the_file_private(tmp_path: Path) -> None:
    store = storage(tmp_path)
    await store.set_tokens(tokens())
    await store.set_tokens(tokens(access_token="access-2"))
    path = tmp_path / "mcp-oauth" / "rh.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["tokens"]["access_token"] == "access-2"


async def test_a_changed_url_ignores_the_stored_registration_and_tokens(
    tmp_path: Path,
) -> None:
    old = storage(tmp_path)
    await old.set_client_info(client_info())
    await old.set_tokens(tokens())

    moved = storage(tmp_path, url="https://other.example.com/mcp")
    assert await moved.get_tokens() is None
    assert await moved.get_client_info() is None


async def test_a_changed_redirect_uri_ignores_the_stored_registration(
    tmp_path: Path,
) -> None:
    # A registration is only good for the redirect URI it was made with.
    old = storage(tmp_path)
    await old.set_client_info(client_info())
    await old.set_tokens(tokens())

    moved = storage(tmp_path, redirect="http://localhost:8130/oauth/callback")
    assert await moved.get_tokens() is None
    assert await moved.get_client_info() is None


async def test_writing_under_a_new_url_replaces_the_old_urls_record(
    tmp_path: Path,
) -> None:
    await storage(tmp_path).set_tokens(tokens())
    moved = storage(tmp_path, url="https://other.example.com/mcp")
    await moved.set_client_info(client_info())

    record = json.loads((tmp_path / "mcp-oauth" / "rh.json").read_text())
    assert record["url"] == "https://other.example.com/mcp"
    assert "tokens" not in record


async def test_clear_deletes_the_file_and_is_safe_to_repeat(tmp_path: Path) -> None:
    store = storage(tmp_path)
    await store.set_tokens(tokens())
    store.clear()
    assert not (tmp_path / "mcp-oauth" / "rh.json").exists()
    assert await store.get_tokens() is None
    store.clear()  # nothing to delete is fine


@pytest.mark.parametrize("name", ["../x", "a/b", "", ".", "..", "a b", "a.b"])
def test_unsafe_server_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="server name"):
        storage(tmp_path, server=name)


async def test_a_corrupt_file_reads_as_no_login_instead_of_crashing(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "mcp-oauth"
    directory.mkdir()
    (directory / "rh.json").write_text("{not json")
    assert await storage(tmp_path).get_tokens() is None


def provider_for(store: FileTokenStorage) -> OAuthClientProvider:
    metadata = OAuthClientMetadata.model_validate({"redirect_uris": [REDIRECT]})
    provider = OAuthClientProvider(URL, metadata, store)
    store.attach(provider.context)
    return provider


async def test_loading_tokens_restores_their_expiry_onto_the_sdk_context(
    tmp_path: Path,
) -> None:
    # The SDK restores tokens without an expiry and then treats them as valid
    # forever; the stored absolute expiry is what lets it refresh instead.
    await storage(tmp_path).set_tokens(tokens(expires_in=3600))
    fresh = storage(tmp_path)
    provider = provider_for(fresh)

    await fresh.get_tokens()

    expiry = provider.context.token_expiry_time
    assert expiry is not None
    assert time.time() + 3500 < expiry < time.time() + 3700


async def test_an_expired_stored_token_is_no_longer_valid_to_the_sdk(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mcp-oauth" / "rh.json"
    await storage(tmp_path).set_tokens(tokens())
    record = json.loads(path.read_text())
    record["expires_at"] = time.time() - 10
    path.write_text(json.dumps(record))

    fresh = storage(tmp_path)
    provider = provider_for(fresh)
    provider.context.current_tokens = await fresh.get_tokens()

    assert not provider.context.is_token_valid()


# -- a margin before the server's own expiry ------------------------------------


def stored_expiry(tmp_path: Path) -> float | None:
    record = json.loads((tmp_path / "mcp-oauth" / "rh.json").read_text())
    expires_at: float | None = record["expires_at"]
    return expires_at


async def test_a_login_expires_a_margin_before_the_server_says(tmp_path: Path) -> None:
    # The SDK trusts a token until its computed expiry, which it computes after
    # the response has arrived, and a request the server refuses never refreshes:
    # it starts a new login. So the token is let go a minute early, and the very
    # value the SDK judges by is what a restart brings back.
    store = storage(tmp_path)
    context = provider_for(store).context
    before = time.time()

    await store.set_tokens(tokens(expires_in=3600))

    assert context.token_expiry_time == pytest.approx(before + 3600 - 60, abs=2)
    assert stored_expiry(tmp_path) == context.token_expiry_time
    fresh = storage(tmp_path)
    restored = provider_for(fresh).context
    await fresh.get_tokens()
    assert restored.token_expiry_time == context.token_expiry_time


@pytest.mark.parametrize(
    ("lifetime", "margin"), [(100, 50), (30, 15), (2, 1), (0, 0)]
)
async def test_a_short_lived_token_gives_up_at_most_half_its_life(
    tmp_path: Path, lifetime: int, margin: int
) -> None:
    store = storage(tmp_path)
    context = provider_for(store).context
    before = time.time()

    await store.set_tokens(tokens(expires_in=lifetime))

    assert context.token_expiry_time == pytest.approx(before + lifetime - margin, abs=2)


async def test_a_token_with_no_lifetime_has_no_expiry(tmp_path: Path) -> None:
    store = storage(tmp_path)
    context = provider_for(store).context
    context.token_expiry_time = 1.0  # what the previous token had

    await store.set_tokens(tokens(expires_in=None))

    assert context.token_expiry_time is None
    assert stored_expiry(tmp_path) is None


@pytest.mark.parametrize("expires_in", [3600, None])
async def test_forcing_a_refresh_makes_the_stored_token_read_as_spent(
    tmp_path: Path, expires_in: int | None
) -> None:
    # What lets a passive connection that was turned away try the refresh token
    # before giving up: it is the stored expiry the next provider judges by.
    await storage(tmp_path).set_tokens(tokens(expires_in=expires_in))
    store = storage(tmp_path)

    assert store.force_refresh() is True

    fresh = storage(tmp_path)
    provider = provider_for(fresh)
    provider.context.current_tokens = await fresh.get_tokens()
    assert provider.context.current_tokens == tokens(expires_in=expires_in)
    assert not provider.context.is_token_valid()  # a positive past, not "no expiry"


async def test_forcing_a_refresh_needs_a_refresh_token_to_do_it_with(
    tmp_path: Path,
) -> None:
    store = storage(tmp_path)
    assert store.force_refresh() is False  # nothing stored at all

    await store.set_tokens(tokens(refresh_token=None))
    saved = (tmp_path / "mcp-oauth" / "rh.json").read_text()

    assert store.force_refresh() is False
    assert (tmp_path / "mcp-oauth" / "rh.json").read_text() == saved


async def test_a_refresh_response_without_a_refresh_token_keeps_the_old_one(
    tmp_path: Path,
) -> None:
    store = storage(tmp_path)
    await store.set_tokens(tokens(refresh_token="refresh-1"))

    rotated = tokens(access_token="access-2", refresh_token=None)
    await store.set_tokens(rotated)

    assert rotated.refresh_token == "refresh-1"  # the SDK's own copy keeps it too
    stored = await storage(tmp_path).get_tokens()
    assert stored is not None
    assert (stored.access_token, stored.refresh_token) == ("access-2", "refresh-1")


async def test_a_rotated_refresh_token_replaces_the_old_one(tmp_path: Path) -> None:
    store = storage(tmp_path)
    await store.set_tokens(tokens(refresh_token="refresh-1"))
    await store.set_tokens(tokens(refresh_token="refresh-2"))
    stored = await storage(tmp_path).get_tokens()
    assert stored is not None
    assert stored.refresh_token == "refresh-2"


# -- what discovery learned ----------------------------------------------------


def as_metadata(token_endpoint: str = TOKEN_ENDPOINT) -> OAuthMetadata:
    return OAuthMetadata.model_validate(
        {
            "issuer": ISSUER,
            "authorization_endpoint": "https://example.com/oauth",
            "token_endpoint": token_endpoint,
            "revocation_endpoint": REVOCATION_ENDPOINT,
        }
    )


def resource_metadata() -> ProtectedResourceMetadata:
    return ProtectedResourceMetadata.model_validate(
        {"resource": URL, "authorization_servers": [ISSUER]}
    )


async def log_in_with_discovery(tmp_path: Path) -> None:
    """Store a login the way the SDK's flow ends one: it has discovered the
    server by the time its tokens arrive."""
    store = storage(tmp_path)
    context = provider_for(store).context
    context.oauth_metadata = as_metadata()
    context.protected_resource_metadata = resource_metadata()
    context.auth_server_url = ISSUER
    await store.set_client_info(client_info())
    await store.set_tokens(tokens())


async def test_what_discovery_found_comes_back_onto_a_fresh_providers_context(
    tmp_path: Path,
) -> None:
    # A restart builds a fresh provider whose first request can be a refresh,
    # before any 401 has sent it discovering. Without the stored endpoints the SDK
    # guesses <mcp origin>/token and hands the refresh token to the wrong host.
    await log_in_with_discovery(tmp_path)
    fresh = storage(tmp_path)
    context = provider_for(fresh).context

    await fresh.get_tokens()

    assert context.oauth_metadata == as_metadata()
    assert context.protected_resource_metadata == resource_metadata()
    assert context.auth_server_url == ISSUER


async def test_a_session_keeps_what_it_discovered_over_what_is_stored(
    tmp_path: Path,
) -> None:
    await log_in_with_discovery(tmp_path)
    fresh = storage(tmp_path)
    context = provider_for(fresh).context
    moved = as_metadata("https://api.example.com/oauth3/token/")
    context.oauth_metadata = moved
    context.auth_server_url = "https://other.example.com"

    await fresh.get_tokens()  # the SDK re-reads after a failed refresh

    assert context.oauth_metadata == moved
    assert context.auth_server_url == "https://other.example.com"


async def test_a_changed_url_does_not_restore_the_old_servers_endpoints(
    tmp_path: Path,
) -> None:
    await log_in_with_discovery(tmp_path)
    moved = storage(tmp_path, url="https://other.example.com/mcp")
    context = provider_for(moved).context

    await moved.get_tokens()

    assert context.oauth_metadata is None
    assert context.protected_resource_metadata is None
    assert context.auth_server_url is None


async def test_unreadable_stored_endpoints_are_ignored_not_fatal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await log_in_with_discovery(tmp_path)
    path = tmp_path / "mcp-oauth" / "rh.json"
    record = json.loads(path.read_text())
    record["oauth_metadata"] = {"token_endpoint": "not a url"}
    path.write_text(json.dumps(record))
    fresh = storage(tmp_path)
    context = provider_for(fresh).context

    with caplog.at_level(logging.WARNING):
        assert await fresh.get_tokens() == tokens()  # the login itself is intact

    assert context.oauth_metadata is None
    assert "unreadable discovery data" in caplog.text


async def test_the_revocation_endpoint_is_the_one_discovery_found(
    tmp_path: Path,
) -> None:
    assert storage(tmp_path).revocation_endpoint() is None
    await log_in_with_discovery(tmp_path)
    assert storage(tmp_path).revocation_endpoint() == REVOCATION_ENDPOINT


# -- a login that was forgotten --------------------------------------------------


async def test_a_session_whose_login_was_forgotten_cannot_write_it_back(
    tmp_path: Path,
) -> None:
    # Logout deletes the file while the connection it ends may still refresh on
    # its way out; that refresh must not resurrect the login.
    live = [True]
    store = FileTokenStorage(
        tmp_path / "mcp-oauth", "rh", URL, REDIRECT, writable=lambda: live[0]
    )
    await store.set_tokens(tokens())
    live[0] = False
    store.clear()

    await store.set_tokens(tokens(access_token="access-2"))
    await store.set_client_info(client_info())

    assert not (tmp_path / "mcp-oauth" / "rh.json").exists()
