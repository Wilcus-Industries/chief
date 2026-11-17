"""A package manifest's ``mcp_servers`` block: shape validation + parsing.

``McpServerSpec`` matches ``config.yaml``'s own ``mcp_servers`` entry (see
``config.Config.mcp_servers``, ``mcpclient.wiring.build_mcp``). Split out of
``chief.pkg`` (issue #238) so both the validate() and parse() sides of
this one concern — and their shared shape checks — have a file of their own.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard


@dataclass(frozen=True)
class McpServerSpec:
    """One MCP server a package declares.

    ``auth`` (only ``"oauth"``), ``headers`` and ``scopes`` apply to ``url``
    servers. ``headers`` is stored as pairs so a spec stays hashable; a secret
    belongs in one only as a ``${secret:name}`` reference, never by value.
    """

    name: str
    url: str | None = None
    command: tuple[str, ...] | None = None
    auth: str | None = None
    headers: tuple[tuple[str, str], ...] | None = None
    scopes: tuple[str, ...] | None = None


# Shape parse_mcp_servers will accept — the only shape tuple(command) may
# see safely. Truthiness alone (the old check) let a scalar or non-iterable
# command validate clean and then mis-parse or raise downstream (#238).
def _is_valid_command(command: object) -> TypeGuard[list[str]]:
    return (
        isinstance(command, list)
        and len(command) > 0
        and all(isinstance(item, str) and item.strip() for item in command)
    )


def _is_valid_url(url: object) -> bool:
    return isinstance(url, str) and bool(url.strip())


def _is_valid_headers(headers: object) -> TypeGuard[dict[str, str]]:
    return isinstance(headers, dict) and all(
        isinstance(name, str) and name.strip() and isinstance(value, str)
        for name, value in headers.items()
    )


def _is_valid_scopes(scopes: object) -> TypeGuard[list[str]]:
    return isinstance(scopes, list) and all(
        isinstance(scope, str) and scope.strip() for scope in scopes
    )


def validate_mcp_servers(manifest: Path, mcp_servers: object) -> list[str]:
    """A declared ``mcp_servers`` block must map to mappings, each setting
    exactly one of ``url`` or ``command`` in the shape ``McpServerSpec``
    expects (``ServerConfig``'s own rule) — malformed fails the done-check
    and is rolled back."""
    if mcp_servers is None:
        return []
    if not isinstance(mcp_servers, dict):
        return [f"{manifest}: 'mcp_servers' must be a mapping"]
    problems = []
    for name, entry in mcp_servers.items():
        problems.extend(_validate_entry(manifest, name, entry))
    return problems


def _validate_entry(manifest: Path, name: object, entry: object) -> list[str]:
    if not isinstance(entry, dict):
        return [f"{manifest}: mcp_servers.{name} must be a mapping"]
    url, command = entry.get("url"), entry.get("command")
    both_or_neither = (
        f"{manifest}: mcp_servers.{name} must set exactly one of 'url' or 'command'"
    )
    if url is not None and command is not None:
        return [both_or_neither]
    if url is not None:
        if not _is_valid_url(url):
            return [f"{manifest}: mcp_servers.{name}.url must be a non-empty string"]
        return _validate_options(f"{manifest}: mcp_servers.{name}", entry, is_url=True)
    if command is not None:
        if not _is_valid_command(command):
            return [
                f"{manifest}: mcp_servers.{name}.command must be a non-empty "
                "list of strings"
            ]
        return _validate_options(f"{manifest}: mcp_servers.{name}", entry, is_url=False)
    return [both_or_neither]


def _validate_options(where: str, entry: dict[str, Any], *, is_url: bool) -> list[str]:
    """The optional ``auth`` / ``headers`` / ``scopes`` keys of one entry."""
    problems = []
    if not is_url:
        problems = [
            f"{where}.{key} applies to url servers only"
            for key in ("auth", "headers")
            if key in entry
        ]
    elif entry.get("auth") not in (None, "oauth"):
        problems.append(f"{where}.auth must be 'oauth' when set")
    if is_url and "headers" in entry and not _is_valid_headers(entry["headers"]):
        problems.append(
            f"{where}.headers must map non-empty names to strings "
            "(secrets only as ${secret:name} references)"
        )
    if "scopes" in entry and not _is_valid_scopes(entry["scopes"]):
        problems.append(f"{where}.scopes must be a list of non-empty strings")
    return problems


def parse_mcp_servers(mcp_servers: object) -> tuple[McpServerSpec, ...]:
    """Build one spec per well-formed entry; drop a malformed block/entry and
    any unrecognized key inside one silently (validate_mcp_servers is the
    loud path).

    Re-checks shape here too, not just in validate(): this also runs over
    the pulled ``data/packages`` clone, which validate() never covers, so a
    scalar/non-iterable ``command`` must be dropped rather than mangled or
    raising ``TypeError`` out of ``PackageLibrary.scan`` (issue #238). A
    malformed ``auth`` / ``headers`` / ``scopes`` value is dropped on its own,
    keeping the entry."""
    if not isinstance(mcp_servers, dict):
        return ()
    specs = []
    for name, entry in mcp_servers.items():
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        command = entry.get("command")
        spec_url = url if _is_valid_url(url) else None
        spec_command = tuple(command) if _is_valid_command(command) else None
        if spec_url is None and spec_command is None:
            continue
        headers, scopes = entry.get("headers"), entry.get("scopes")
        specs.append(
            McpServerSpec(
                name=str(name),
                url=spec_url,
                command=spec_command,
                auth="oauth" if spec_url and entry.get("auth") == "oauth" else None,
                headers=(
                    tuple(headers.items())
                    if spec_url and _is_valid_headers(headers)
                    else None
                ),
                scopes=tuple(scopes) if _is_valid_scopes(scopes) else None,
            )
        )
    return tuple(specs)
