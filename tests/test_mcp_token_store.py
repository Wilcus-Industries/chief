"""FileTokenStorage: the per-server OAuth token + client-registration file."""

import json
import stat
import time
from pathlib import Path

import pytest
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

from chief.mcpclient.token_store import FileTokenStorage

URL = "https://agent.example.com/mcp/trading"
REDIRECT = "http://127.0.0.1:8130/oauth/callback"


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
    return OAuthClientInformationFull(
        client_id="client-1", redirect_uris=[AnyUrl(REDIRECT)],
        token_endpoint_auth_method="none",
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
    provider = OAuthClientProvider(
        URL, OAuthClientMetadata(redirect_uris=[AnyUrl(REDIRECT)]), store
    )
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
