"""``mcp_servers`` entries: the ``auth`` and ``scopes`` keys."""

import logging

import pytest

from chief.mcpclient.config import ServerConfig, server_config_from_entry


def test_oauth_entry_parses_auth_and_scopes() -> None:
    config = server_config_from_entry(
        "rh", {"url": "https://x/mcp", "auth": "oauth", "scopes": ["internal", "x"]}
    )
    assert config == ServerConfig(
        name="rh", url="https://x/mcp", auth="oauth", scopes=("internal", "x")
    )


def test_entry_without_them_keeps_both_unset() -> None:
    config = server_config_from_entry("plain", {"url": "https://x/mcp"})
    assert config is not None
    assert (config.auth, config.scopes) == (None, None)


@pytest.mark.parametrize("auth", ["bearer", "OAuth", True, ["oauth"], ""])
def test_unknown_auth_value_skips_the_server(
    auth: object, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        config = server_config_from_entry("rh", {"url": "https://x/mcp", "auth": auth})
    assert config is None
    assert "rh" in caplog.text and "auth" in caplog.text


def test_auth_on_a_stdio_server_skips_it(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        config = server_config_from_entry("rh", {"command": ["x"], "auth": "oauth"})
    assert config is None
    assert "rh" in caplog.text


@pytest.mark.parametrize("scopes", ["internal", [1], [""], {"a": "b"}])
def test_malformed_scopes_skip_the_server(
    scopes: object, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        config = server_config_from_entry(
            "rh", {"url": "https://x/mcp", "auth": "oauth", "scopes": scopes}
        )
    assert config is None
    assert "scopes" in caplog.text


@pytest.mark.parametrize("name", ["a/b", "../x", "a b", "a.b"])
def test_an_oauth_server_name_must_be_a_safe_filename(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    # The name becomes secrets/mcp-oauth/<name>.json.
    with caplog.at_level(logging.WARNING):
        entry = {"url": "https://x/mcp", "auth": "oauth"}
        config = server_config_from_entry(name, entry)
    assert config is None
    assert "name" in caplog.text
