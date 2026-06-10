"""Regression tests for docker-compose.yml invariants (issue #16, #33).

These tests parse the compose file directly so CI catches config regressions
without needing Docker. They cover the read-only-rootfs writable-path contract
(issue #16) and the playwright-network SSRF isolation contract (issue #33) —
neither requires Docker at runtime.
"""

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

_COMPOSE_PATH = Path(__file__).parent.parent / "docker-compose.yml"

# Network names used in the SSRF isolation design (issue #33).
_PLAYWRIGHT_NET = "playwright-net"
_MCP_INTERNAL_NET = "mcp-internal"

# Google MCP services that must stay off the playwright network.
_GOOGLE_MCP_SERVICES = {"mcp-calendar", "mcp-drive", "mcp-sheets", "mcp-gmail"}


def _load_core_env() -> dict[str, str]:
    """Return the core service environment block as a flat dict."""
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    raw = compose["services"]["core"]["environment"]
    # docker-compose environment can be a list of "KEY=VAL" strings or a mapping.
    if isinstance(raw, dict):
        return {k: str(v) if v is not None else "" for k, v in raw.items()}
    env: dict[str, str] = {}
    for entry in raw:
        if "=" in entry:
            key, _, val = entry.partition("=")
            env[key] = val
        else:
            env[entry] = ""
    return env


def test_claude_config_dir_is_set_on_core_service() -> None:
    """CLAUDE_CONFIG_DIR must be set for the core service.

    Without it the claude CLI writes .claude.json directly under $HOME
    (/home/chief/.claude.json), which is on the read-only rootfs → EROFS.
    Setting CLAUDE_CONFIG_DIR to a path inside the persisted claude-home
    volume relocates the write to a writable mount.
    """
    env = _load_core_env()
    assert "CLAUDE_CONFIG_DIR" in env, (
        "core service is missing CLAUDE_CONFIG_DIR — the claude CLI will "
        "attempt to write .claude.json on the read-only rootfs (EROFS). "
        "Add CLAUDE_CONFIG_DIR: /home/chief/.claude to the core environment."
    )


def test_claude_config_dir_points_inside_claude_home_volume() -> None:
    """CLAUDE_CONFIG_DIR must resolve to a path covered by the claude-home volume.

    The claude-home volume is mounted at /home/chief/.claude.  The env var must
    point at or inside that directory so .claude.json lands on the volume
    (writable + persisted across recreates) rather than on the rootfs.
    """
    env = _load_core_env()
    config_dir = env.get("CLAUDE_CONFIG_DIR", "")
    claude_home_mount = "/home/chief/.claude"
    assert config_dir == claude_home_mount or config_dir.startswith(
        claude_home_mount + "/"
    ), (
        f"CLAUDE_CONFIG_DIR={config_dir!r} does not resolve inside the "
        f"claude-home volume ({claude_home_mount}). .claude.json will land "
        "outside the volume and either hit the read-only rootfs (EROFS) or "
        "be lost on a recreate."
    )


def _service_networks(compose: dict[str, Any], service: str) -> set[str]:
    """Return the set of network names attached to *service* in *compose*.

    Handles both the list form (``networks: [a, b]``) and the mapping form
    (``networks: {a: ..., b: ...}``) that docker-compose allows.  Returns an
    empty set when the service has no explicit ``networks`` key (which means it
    is attached to the implicit default network, not to any named network we
    care about here).
    """
    raw = compose["services"][service].get("networks")
    if raw is None:
        return set()
    if isinstance(raw, list):
        return set(raw)
    # mapping form: {net_name: {aliases: [...], ...} | null}
    return set(raw.keys())


def test_claude_home_volume_mounted_on_core() -> None:
    """The claude-home named volume must be mounted in the core service.

    Without this mount, CLAUDE_CONFIG_DIR=/home/chief/.claude would still
    point at the read-only rootfs (the directory would not be a volume mount
    point) and every write would fail with EROFS.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    core_volumes: list[str] = compose["services"]["core"].get("volumes", [])
    has_claude_home = any(
        (isinstance(v, str) and "claude-home" in v)
        or (isinstance(v, dict) and v.get("source") == "claude-home")
        for v in core_volumes
    )
    assert has_claude_home, (
        "claude-home volume is not mounted in the core service. "
        "CLAUDE_CONFIG_DIR=/home/chief/.claude will write to the "
        "read-only rootfs instead of the persisted volume."
    )


# ---------------------------------------------------------------------------
# Network-isolation invariants (issue #33 — SSRF hardening for playwright)
# ---------------------------------------------------------------------------


def test_networks_block_declares_both_isolation_networks() -> None:
    """Both named networks must be declared at the top-level networks key.

    Without explicit declarations the services cannot reference them.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    top_networks: set[str] = set(compose.get("networks", {}).keys())
    assert _PLAYWRIGHT_NET in top_networks, (
        f"Top-level networks block is missing {_PLAYWRIGHT_NET!r}. "
        "Add it so the playwright container has its own isolated network."
    )
    assert _MCP_INTERNAL_NET in top_networks, (
        f"Top-level networks block is missing {_MCP_INTERNAL_NET!r}. "
        "Add it so the Google MCP services share a dedicated internal network."
    )


def test_mcp_playwright_is_on_playwright_net() -> None:
    """mcp-playwright must be attached to the dedicated playwright network.

    This gives it internet egress while keeping it off the MCP-internal network
    where the Google MCP services live.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    nets = _service_networks(compose, "mcp-playwright")
    assert _PLAYWRIGHT_NET in nets, (
        f"mcp-playwright is not attached to {_PLAYWRIGHT_NET!r}. "
        "Attach it so that core can reach it for MCP calls."
    )


def test_mcp_playwright_is_not_on_mcp_internal() -> None:
    """mcp-playwright must NOT be on the shared MCP-internal network.

    A compromised or hostile page must not be able to SSRF into the Google MCP
    services (calendar, drive, sheets, gmail) via the playwright container.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    nets = _service_networks(compose, "mcp-playwright")
    assert _MCP_INTERNAL_NET not in nets, (
        f"mcp-playwright is attached to {_MCP_INTERNAL_NET!r} — this allows "
        "SSRF from a hostile page into the Google MCP services. Remove it."
    )


def test_core_is_on_both_networks() -> None:
    """core must be attached to both networks so it can reach all MCP services.

    core talks to the Google MCP services over mcp-internal and to the browser
    over playwright-net; it must sit on both.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    nets = _service_networks(compose, "core")
    assert _PLAYWRIGHT_NET in nets, (
        f"core is not on {_PLAYWRIGHT_NET!r} — it cannot reach mcp-playwright."
    )
    assert _MCP_INTERNAL_NET in nets, (
        f"core is not on {_MCP_INTERNAL_NET!r} — it cannot reach the Google "
        "MCP services."
    )


def test_google_mcp_services_are_on_mcp_internal_only() -> None:
    """Each Google MCP service must be on mcp-internal and NOT on playwright-net.

    Keeping them off playwright-net is the SSRF isolation guarantee: a
    compromised playwright container has no route to these services.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    for svc in _GOOGLE_MCP_SERVICES:
        nets = _service_networks(compose, svc)
        assert _MCP_INTERNAL_NET in nets, (
            f"{svc} is not attached to {_MCP_INTERNAL_NET!r}. "
            "core cannot reach it for MCP calls."
        )
        assert _PLAYWRIGHT_NET not in nets, (
            f"{svc} is attached to {_PLAYWRIGHT_NET!r} — this breaks the SSRF "
            "isolation. Remove playwright-net from this service."
        )
