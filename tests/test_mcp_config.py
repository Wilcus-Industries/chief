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


def test_session_context_rides_along_with_the_oauth_and_header_keys() -> None:
    config = server_config_from_entry(
        "rh",
        {
            "url": "https://x/mcp", "auth": "oauth", "scopes": ["internal"],
            "headers": {"X-Api": "v"}, "timeout": 5, "session_context": True,
        },
    )
    assert config == ServerConfig(
        name="rh", url="https://x/mcp", auth="oauth", scopes=("internal",),
        headers={"X-Api": "v"}, timeout=5.0, session_context=True,
    )


@pytest.mark.parametrize(
    "bad",
    [{"timeout": "soon"}, {"timeout": 0}, {"headers": ["x"]}, {"auth": "bearer"}],
)
def test_a_bad_key_skips_the_server_even_when_it_opts_in_to_session_context(
    bad: dict[str, object],
) -> None:
    entry = {"url": "https://x/mcp", "session_context": True, **bad}
    assert server_config_from_entry("rh", entry) is None


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


def test_tool_filters_parse_as_glob_tuples() -> None:
    config = server_config_from_entry(
        "rh",
        {
            "url": "https://x/mcp",
            "include_tools": ["get_*", "search"],
            "exclude_tools": ["get_bank*"],
        },
    )
    assert config is not None
    assert config.include_tools == ("get_*", "search")
    assert config.exclude_tools == ("get_bank*",)


def test_tool_filters_default_to_every_tool() -> None:
    config = server_config_from_entry("plain", {"command": ["x"]})
    assert config is not None
    assert (config.include_tools, config.exclude_tools) == (None, ())
    assert config.tool_enabled("anything")


@pytest.mark.parametrize("key", ["include_tools", "exclude_tools"])
@pytest.mark.parametrize("bad", ["get_*", [""], ["ok", 3], {"a": 1}])
def test_a_malformed_tool_filter_skips_the_server(
    key: str, bad: object, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        config = server_config_from_entry("rh", {"url": "https://x/mcp", key: bad})
    assert config is None
    assert key in caplog.text


def test_an_empty_include_list_keeps_nothing() -> None:
    config = server_config_from_entry(
        "rh", {"url": "https://x/mcp", "include_tools": []}
    )
    assert config is not None
    assert not config.tool_enabled("get_portfolio")


@pytest.mark.parametrize(
    ("include", "exclude", "name", "enabled"),
    [
        (None, (), "place_equity_order", True),
        (("get_*",), (), "get_portfolio", True),
        (("get_*",), (), "place_equity_order", False),
        (None, ("place_*",), "place_equity_order", False),
        (None, ("place_*",), "get_portfolio", True),
        (("get_*",), ("get_bank*",), "get_bank_accounts", False),
        (("get_*",), ("get_bank*",), "get_accounts", True),
        # Case-sensitive, like the typesafe screen_tools globs.
        (("GET_*",), (), "get_portfolio", False),
    ],
)
def test_tool_enabled_applies_include_then_exclude(
    include: tuple[str, ...] | None, exclude: tuple[str, ...], name: str, enabled: bool
) -> None:
    config = ServerConfig(
        name="rh", url="https://x/mcp", include_tools=include, exclude_tools=exclude
    )
    assert config.tool_enabled(name) is enabled
