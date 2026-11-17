"""Static MCP headers: ``${secret:name}`` expansion and delivery to a server."""

import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from chief.mcpclient.config import server_config_from_entry
from chief.mcpclient.headers import expand_headers
from chief.mcpclient.manager import McpManager, ServerConfig
from chief.provider.base import ToolCall
from chief.tools import ToolRegistry

from .fake_oauth import FakeOAuth, serve_fake


def reader(files: dict[str, str]) -> Callable[[Path], str]:
    """A ``read`` stand-in mapping secret name -> contents ('' when absent)."""
    return lambda path: files.get(path.name, "")


def test_literal_values_pass_through_untouched() -> None:
    headers = {"X-Api-Key": "plain", "Accept": "application/json"}
    assert expand_headers(headers) == headers


def test_secret_ref_reads_the_upper_cased_env_var_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROBINHOOD_KEY", "from-env")
    result = expand_headers(
        {"Authorization": "Bearer ${secret:robinhood_key}"},
        read=reader({"robinhood_key": "from-file"}),
    )
    assert result == {"Authorization": "Bearer from-env"}


def test_secret_ref_falls_back_to_the_secrets_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ROBINHOOD_KEY", raising=False)
    result = expand_headers(
        {"X-Key": "${secret:robinhood_key}"},
        read=reader({"robinhood_key": "from-file"}),
    )
    assert result == {"X-Key": "from-file"}


def test_secrets_file_is_resolved_under_secrets_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("API_TOKEN", raising=False)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "api_token").write_text("tok\n")
    assert expand_headers({"X": "${secret:api_token}"}) == {"X": "tok"}


def test_several_refs_in_one_value_all_expand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USER_ID", "u1")
    monkeypatch.setenv("PASS_WORD", "p1")
    result = expand_headers({"X": "${secret:user_id}:${secret:pass_word}"})
    assert result == {"X": "u1:p1"}


def test_other_interpolation_is_left_literal() -> None:
    headers = {"X": "${HOME} ${env:HOME} $secret:x ${secret}"}
    assert expand_headers(headers) == headers


def test_missing_secret_raises_naming_the_header_not_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOPE_KEY", raising=False)
    with pytest.raises(ValueError, match=r"X-Api-Key.*nope_key"):
        expand_headers({"X-Api-Key": "${secret:nope_key}"}, read=reader({}))


def test_empty_secret_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMPTY_KEY", "")
    with pytest.raises(ValueError, match="empty_key"):
        expand_headers({"X": "${secret:empty_key}"}, read=reader({"empty_key": ""}))


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b", "a.b", "", "a b"])
def test_secret_names_that_could_escape_the_secrets_dir_are_refused(
    name: str,
) -> None:
    with pytest.raises(ValueError, match="secret"):
        expand_headers({"X": "${secret:" + name + "}"})


def test_config_entry_expands_headers_into_the_server_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SVC_KEY", "k1")
    config = server_config_from_entry(
        "svc", {"url": "https://x/mcp", "headers": {"X-Key": "${secret:svc_key}"}}
    )
    assert config == ServerConfig(
        name="svc", url="https://x/mcp", headers={"X-Key": "k1"}
    )


def test_config_entry_with_a_missing_secret_is_skipped_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SVC_KEY", raising=False)
    entry = {"url": "https://x/mcp", "headers": {"X-Key": "${secret:svc_key}"}}
    with caplog.at_level(logging.WARNING):
        assert server_config_from_entry("svc", entry) is None
    assert "svc" in caplog.text and "X-Key" in caplog.text


@pytest.mark.parametrize(
    "headers",
    [["not", "a", "mapping"], {"X": 5}, {"": "v"}, {"X": None}, "X: y"],
)
def test_config_entry_with_malformed_headers_is_skipped(
    headers: object, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        config = server_config_from_entry(
            "svc", {"url": "https://x/mcp", "headers": headers}
        )
    assert config is None
    assert "svc" in caplog.text


def test_config_entry_headers_on_a_stdio_server_are_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        config = server_config_from_entry(
            "svc", {"command": ["x"], "headers": {"X": "y"}}
        )
    assert config is None
    assert "svc" in caplog.text


def test_server_config_repr_never_shows_header_values() -> None:
    config = ServerConfig(name="svc", url="https://x", headers={"X": "s3cr3t"})
    assert "s3cr3t" not in repr(config)


@pytest.fixture
async def fake() -> AsyncIterator[FakeOAuth]:
    async with serve_fake(
        oauth_enabled=False, required_headers={"x-api-key": "s3cret"}
    ) as server:
        yield server


async def test_configured_headers_reach_the_server(fake: FakeOAuth) -> None:
    registry = ToolRegistry()
    manager = McpManager(registry)
    try:
        count = await manager.connect(
            ServerConfig(
                name="svc", url=fake.mcp_url, headers={"X-Api-Key": "s3cret"}
            )
        )
        assert count == 1
        result = await registry.dispatch(
            ToolCall(id="1", name="mcp_svc_ping", arguments={})
        )
        assert result == "pong"
        assert {h.get("x-api-key") for h in fake.seen_headers} == {"s3cret"}
    finally:
        await manager.stop()


async def test_a_server_that_wants_a_header_we_did_not_send_fails_to_start(
    fake: FakeOAuth,
) -> None:
    manager = McpManager(ToolRegistry())
    try:
        with pytest.raises(RuntimeError, match="failed to start"):
            await manager.connect(ServerConfig(name="svc", url=fake.mcp_url))
    finally:
        await manager.stop()
