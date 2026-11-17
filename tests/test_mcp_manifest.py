"""A package manifest's ``mcp_servers``: the OAuth / header / scope fields."""

from pathlib import Path
from typing import Any

import pytest

from chief.mcp_manifest import McpServerSpec, parse_mcp_servers, validate_mcp_servers
from chief.pkg import PackageLibrary, validate

MANIFEST = Path("manifest.yaml")


def problems(servers: dict[str, Any]) -> list[str]:
    return validate_mcp_servers(MANIFEST, servers)


def test_oauth_url_entry_with_headers_and_scopes_validates_clean() -> None:
    entry = {
        "url": "https://example.com/mcp",
        "auth": "oauth",
        "scopes": ["internal", "trade"],
        "headers": {"X-Client": "chief", "Authorization": "Bearer ${secret:tok}"},
    }
    assert problems({"remote": entry}) == []


def test_url_entry_without_the_new_fields_still_validates_clean() -> None:
    assert problems({"remote": {"url": "https://example.com/mcp"}}) == []


@pytest.mark.parametrize("auth", ["bearer", "OAuth", True, ["oauth"], ""])
def test_unknown_auth_value_is_reported(auth: object) -> None:
    result = problems({"remote": {"url": "https://x/mcp", "auth": auth}})
    assert len(result) == 1
    assert "mcp_servers.remote.auth" in result[0]


@pytest.mark.parametrize("key", ["auth", "headers"])
def test_auth_and_headers_on_a_command_entry_are_reported(key: str) -> None:
    value: object = "oauth" if key == "auth" else {"X": "y"}
    result = problems({"local": {"command": ["npx", "srv"], key: value}})
    assert len(result) == 1
    assert f"mcp_servers.local.{key}" in result[0]
    assert "url" in result[0]


@pytest.mark.parametrize(
    "headers",
    [["X", "y"], "X: y", {"": "v"}, {"X": 5}, {"X": None}, {5: "v"}],
)
def test_malformed_headers_are_reported(headers: object) -> None:
    result = problems({"remote": {"url": "https://x/mcp", "headers": headers}})
    assert len(result) == 1
    assert "mcp_servers.remote.headers" in result[0]


@pytest.mark.parametrize("scopes", ["internal", [1], [""], {"a": "b"}])
def test_malformed_scopes_are_reported(scopes: object) -> None:
    result = problems({"remote": {"url": "https://x/mcp", "scopes": scopes}})
    assert len(result) == 1
    assert "mcp_servers.remote.scopes" in result[0]


def test_parse_carries_auth_headers_and_scopes_through() -> None:
    (spec,) = parse_mcp_servers(
        {
            "remote": {
                "url": "https://x/mcp",
                "auth": "oauth",
                "scopes": ["internal"],
                "headers": {"X-Client": "chief"},
            }
        }
    )
    assert spec == McpServerSpec(
        name="remote",
        url="https://x/mcp",
        auth="oauth",
        scopes=("internal",),
        headers=(("X-Client", "chief"),),
    )


def test_parse_defaults_the_new_fields_to_none() -> None:
    (spec,) = parse_mcp_servers({"remote": {"url": "https://x/mcp"}})
    assert (spec.auth, spec.scopes, spec.headers) == (None, None, None)


def test_parse_drops_malformed_new_fields_but_keeps_the_entry() -> None:
    (spec,) = parse_mcp_servers(
        {
            "remote": {
                "url": "https://x/mcp",
                "auth": "bearer",
                "scopes": "internal",
                "headers": ["nope"],
            }
        }
    )
    assert spec == McpServerSpec(name="remote", url="https://x/mcp")


def test_parse_ignores_auth_and_headers_on_a_command_entry() -> None:
    (spec,) = parse_mcp_servers(
        {"local": {"command": ["npx"], "auth": "oauth", "headers": {"X": "y"}}}
    )
    assert spec == McpServerSpec(name="local", command=("npx",))


def test_specs_with_headers_stay_hashable() -> None:
    (spec,) = parse_mcp_servers(
        {"remote": {"url": "https://x/mcp", "headers": {"X": "y"}}}
    )
    assert {spec} == {spec}


def test_a_real_manifest_with_the_new_fields_round_trips(tmp_path: Path) -> None:
    pkg = tmp_path / "withauth"
    pkg.mkdir()
    (pkg / "manifest.yaml").write_text(
        "name: withauth\ndescription: d\nmcp_servers:\n"
        "  remote:\n    url: https://x/mcp\n    auth: oauth\n"
        "    scopes: [internal]\n"
    )
    package = PackageLibrary((tmp_path,)).get("withauth")
    assert package is not None
    assert package.mcp_servers == (
        McpServerSpec(
            name="remote", url="https://x/mcp", auth="oauth", scopes=("internal",)
        ),
    )
    assert validate((tmp_path,)) == []
