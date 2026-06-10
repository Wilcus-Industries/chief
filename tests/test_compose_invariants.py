"""Regression tests for docker-compose.yml invariants (issue #16, #33, #40).

These tests parse the compose file directly so CI catches config regressions
without needing Docker. They cover the read-only-rootfs writable-path contract
(issue #16), the playwright-network SSRF isolation contract (issue #33), and
the sandbox network reachability contract (issue #40) — none require Docker at
runtime.
"""

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

_COMPOSE_PATH = Path(__file__).parent.parent / "docker-compose.yml"

# Network names used in the SSRF isolation design (issue #33, #42).
_PLAYWRIGHT_NET = "playwright-net"
_MCP_INTERNAL_NET = "mcp-internal"
_SANDBOX_NET = "sandbox-net"

# Google MCP services that must stay off the playwright network and sandbox-net.
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


def test_networks_block_declares_all_isolation_networks() -> None:
    """All three named networks must be declared at the top-level networks key.

    Without explicit declarations the services cannot reference them.
    mcp-internal: shared by core + Google MCP services.
    playwright-net: shared by core + mcp-playwright.
    sandbox-net: dedicated two-member network for core + sandbox (issue #42).
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
    assert _SANDBOX_NET in top_networks, (
        f"Top-level networks block is missing {_SANDBOX_NET!r}. "
        "Add it so core and sandbox share a dedicated two-member network (issue #42)."
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


# ---- screenshots shared volume (issue #34) ------------------------------------


def _service_volumes(service_name: str) -> list[str]:
    """Return the volumes list for a compose service."""
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    result: list[str] = compose["services"][service_name].get("volumes", [])
    return result


def _volume_sources(volumes: list[str]) -> set[str]:
    """Return the named-volume sources from a volumes list (strips target/mode)."""
    sources: set[str] = set()
    for v in volumes:
        if isinstance(v, str):
            # "name:/path[:mode]"
            parts = v.split(":")
            if len(parts) >= 2:
                sources.add(parts[0])
        elif isinstance(v, dict):
            src = v.get("source")
            if src:
                sources.add(src)
    return sources


def _volume_targets(volumes: list[str]) -> dict[str, str]:
    """Return {target_path: source_name} from a volumes list."""
    targets: dict[str, str] = {}
    for v in volumes:
        if isinstance(v, str):
            parts = v.split(":")
            if len(parts) >= 2:
                targets[parts[1]] = parts[0]
        elif isinstance(v, dict):
            src = v.get("source", "")
            tgt = v.get("target", "")
            if src and tgt:
                targets[tgt] = src
    return targets


def test_screenshots_volume_declared_in_top_level_volumes() -> None:
    """A named 'screenshots' volume must be declared at the top-level volumes key.

    Without this declaration the per-service mounts refer to an undeclared volume,
    which Docker Compose rejects at start time.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    top_volumes = compose.get("volumes", {}) or {}
    assert "screenshots" in top_volumes, (
        "'screenshots' is not declared in the top-level volumes block. "
        "Add 'screenshots:' under volumes: in docker-compose.yml."
    )


def test_screenshots_volume_mounted_in_mcp_playwright() -> None:
    """The screenshots volume must be mounted in the mcp-playwright service.

    The playwright server writes screenshots to --output-dir; that dir must be
    the shared volume mount so core can read the files.
    """
    volumes = _service_volumes("mcp-playwright")
    sources = _volume_sources(volumes)
    assert "screenshots" in sources, (
        "'screenshots' volume is not mounted in the mcp-playwright service. "
        "Add '- screenshots:/screenshots' to mcp-playwright.volumes."
    )


def test_screenshots_volume_mounted_in_core() -> None:
    """The screenshots volume must be mounted in the core service.

    Core reads screenshot files from this volume to deliver them via send_file;
    the mount must exist for the path-mapping to resolve to a readable path.
    """
    volumes = _service_volumes("core")
    sources = _volume_sources(volumes)
    assert "screenshots" in sources, (
        "'screenshots' volume is not mounted in the core service. "
        "Add '- screenshots:/screenshots:ro' to core.volumes."
    )


def test_screenshots_volume_same_target_in_both_services() -> None:
    """The screenshots volume must be mounted at the SAME path in both services.

    The path-mapping logic converts a filename to an absolute path; if the
    mount point differs between mcp-playwright and core the path would resolve
    incorrectly in core.
    """
    pw_targets = _volume_targets(_service_volumes("mcp-playwright"))
    core_targets = _volume_targets(_service_volumes("core"))
    # Find the target where screenshots volume is mounted in each service.
    pw_mount = next(
        (t for t, s in pw_targets.items() if s == "screenshots"), None
    )
    core_mount = next(
        (t for t, s in core_targets.items() if s == "screenshots"), None
    )
    assert pw_mount is not None, "screenshots volume not found in mcp-playwright"
    assert core_mount is not None, "screenshots volume not found in core"
    # Strip trailing :ro / :rw — only the path matters.
    pw_path = pw_mount.split(":")[0]
    core_path = core_mount.split(":")[0]
    assert pw_path == core_path, (
        f"screenshots volume mounted at different paths: "
        f"mcp-playwright={pw_path!r}, core={core_path!r}. "
        "Both must use the same mount path so path-mapping works."
    )


def test_playwright_dockerfile_sets_output_dir() -> None:
    """The mcp-playwright Dockerfile CMD must include --output-dir.

    Without this flag the playwright server writes screenshots to an
    unpredictable temp directory, not the shared volume mount.
    """
    dockerfile = (
        Path(__file__).parent.parent / "docker" / "mcp-playwright" / "Dockerfile"
    )
    content = dockerfile.read_text()
    assert "--output-dir" in content, (
        "docker/mcp-playwright/Dockerfile CMD does not include --output-dir. "
        "Add '--output-dir', '/screenshots' to the CMD so screenshots land in "
        "the shared volume."
    )


# ---- browser-profile persistent volume (issue #35) ----------------------------

_BROWSER_PROFILE_VOLUME = "browser-profile"
_BROWSER_PROFILE_MOUNT = "/browser-profile"


def test_browser_profile_volume_declared_in_top_level_volumes() -> None:
    """A named 'browser-profile' volume must be declared at the top-level volumes key.

    Without this declaration the mount in mcp-playwright refers to an undeclared
    volume, which Docker Compose rejects at start time.  A named volume (not tmpfs
    or an anonymous inline volume) survives container restarts, giving cookie-based
    logins the persistence they need.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    top_volumes = compose.get("volumes", {}) or {}
    assert _BROWSER_PROFILE_VOLUME in top_volumes, (
        f"'{_BROWSER_PROFILE_VOLUME}' is not declared in the top-level volumes block. "
        f"Add '{_BROWSER_PROFILE_VOLUME}:' under volumes: in docker-compose.yml."
    )


def test_browser_profile_volume_mounted_in_mcp_playwright() -> None:
    """The browser-profile volume must be mounted (writable) in mcp-playwright.

    Chromium writes its full profile (cookies, localStorage, IndexedDB, session
    state) to --user-data-dir on every page visit, so the volume must be writable.
    A read-only mount would make Chromium unable to persist any state.
    """
    volumes = _service_volumes("mcp-playwright")
    targets = _volume_targets(volumes)
    # The volume must be mounted at _BROWSER_PROFILE_MOUNT without :ro.
    mount_path = next(
        (t for t, s in targets.items() if s == _BROWSER_PROFILE_VOLUME), None
    )
    assert mount_path is not None, (
        f"'{_BROWSER_PROFILE_VOLUME}' volume is not mounted in the mcp-playwright "
        f"service.  Add '- {_BROWSER_PROFILE_VOLUME}:{_BROWSER_PROFILE_MOUNT}' to "
        "mcp-playwright.volumes."
    )
    # Strip any mode suffix and verify the mount point.
    path = mount_path.split(":")[0]
    assert path == _BROWSER_PROFILE_MOUNT, (
        f"'{_BROWSER_PROFILE_VOLUME}' volume is mounted at {path!r} but must be at "
        f"{_BROWSER_PROFILE_MOUNT!r} so the --user-data-dir flag path is consistent."
    )
    # The mount must be writable (no :ro suffix).
    raw = next(
        v for v in volumes if isinstance(v, str) and _BROWSER_PROFILE_VOLUME in v
    )
    assert ":ro" not in raw, (
        f"'{_BROWSER_PROFILE_VOLUME}' volume is mounted read-only in mcp-playwright. "
        "It must be writable so Chromium can persist cookies and session state."
    )


def test_playwright_dockerfile_sets_user_data_dir_flag() -> None:
    """The mcp-playwright Dockerfile CMD must include --user-data-dir.

    --user-data-dir <path> tells Chromium to persist its full profile (cookies,
    localStorage, IndexedDB, session state) to disk.  Because all writes go
    directly through Chromium, logins survive container restarts by construction
    — no explicit save step required.  The path must be on the browser-profile
    named volume so it outlives the container.
    """
    dockerfile = (
        Path(__file__).parent.parent / "docker" / "mcp-playwright" / "Dockerfile"
    )
    content = dockerfile.read_text()
    assert "--user-data-dir" in content, (
        "docker/mcp-playwright/Dockerfile CMD does not include --user-data-dir. "
        f"Add '--user-data-dir', '{_BROWSER_PROFILE_MOUNT}' to the CMD so Chromium "
        "persists its profile to the browser-profile named volume."
    )


def test_playwright_dockerfile_does_not_use_isolated_flag() -> None:
    """The mcp-playwright Dockerfile CMD must NOT include --isolated.

    --isolated keeps browser state in memory only and discards it on session
    close — the opposite of persistence.  When --user-data-dir is used,
    --isolated must be absent so Chromium writes profile data to disk.
    """
    dockerfile = (
        Path(__file__).parent.parent / "docker" / "mcp-playwright" / "Dockerfile"
    )
    content = dockerfile.read_text()
    assert "--isolated" not in content, (
        "docker/mcp-playwright/Dockerfile CMD includes --isolated, which discards "
        "all browser state on session close and prevents login persistence. "
        "Remove --isolated and use --user-data-dir instead."
    )


def test_playwright_dockerfile_does_not_use_storage_state_flag() -> None:
    """The mcp-playwright Dockerfile CMD must NOT include --storage-state.

    --storage-state is a one-way read-only seed — it never writes back, so
    logins made during a session are not persisted.  With --user-data-dir,
    Chromium handles persistence natively; --storage-state is redundant and
    misleading.
    """
    dockerfile = (
        Path(__file__).parent.parent / "docker" / "mcp-playwright" / "Dockerfile"
    )
    content = dockerfile.read_text()
    assert "--storage-state" not in content, (
        "docker/mcp-playwright/Dockerfile CMD includes --storage-state, which is "
        "a read-only seed incompatible with --user-data-dir persistence. "
        "Remove --storage-state from the CMD."
    )


# ---- sandbox network isolation (issue #40 regression fix, issue #42) ----------


def test_sandbox_is_not_on_mcp_internal() -> None:
    """sandbox must NOT be attached to mcp-internal (issue #42 security fix).

    mcp-internal is shared by core and the four Google MCP sidecars
    (mcp-calendar, mcp-drive, mcp-sheets, mcp-gmail).  Those endpoints are
    unauthenticated — network reachability is the only access control.  A
    sandbox on mcp-internal can reach them via arbitrary bash commands, bypassing
    core's permission gate entirely.  Use the dedicated sandbox-net instead.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    nets = _service_networks(compose, "sandbox")
    assert _MCP_INTERNAL_NET not in nets, (
        f"sandbox is attached to {_MCP_INTERNAL_NET!r} — a bash command in the "
        "sandbox can reach the unauthenticated Google MCP services (gmail, drive, "
        "sheets, calendar) and act on the owner's account without going through "
        f"core's permission gate.  Remove {_MCP_INTERNAL_NET!r} from sandbox and "
        f"use the dedicated {_SANDBOX_NET!r} instead."
    )


def test_sandbox_is_not_on_playwright_net() -> None:
    """sandbox must NOT be on playwright-net.

    The sandbox is a secret-free worker; keeping it off playwright-net prevents
    a compromised browser container from reaching the sandbox shell over HTTP.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    nets = _service_networks(compose, "sandbox")
    assert _PLAYWRIGHT_NET not in nets, (
        f"sandbox is attached to {_PLAYWRIGHT_NET!r}. "
        "Remove playwright-net from sandbox — the sandbox shell must not be "
        "reachable from the browser container."
    )


def test_core_and_sandbox_share_sandbox_net() -> None:
    """core and sandbox must both be on the dedicated sandbox-net (issue #42).

    core reaches the sandbox shell server via DNS name ``sandbox:8765``;
    Docker's embedded DNS only resolves service names within a shared network.
    sandbox-net is a two-member network (core + sandbox only) — it gives core
    the route it needs while keeping sandbox isolated from the Google MCP services
    that live on mcp-internal.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    core_nets = _service_networks(compose, "core")
    sandbox_nets = _service_networks(compose, "sandbox")
    assert _SANDBOX_NET in core_nets, (
        f"core is not on {_SANDBOX_NET!r} — it cannot reach sandbox:8765. "
        f"Add {_SANDBOX_NET!r} to the core service networks."
    )
    assert _SANDBOX_NET in sandbox_nets, (
        f"sandbox is not on {_SANDBOX_NET!r} — core cannot reach sandbox:8765. "
        f"Add {_SANDBOX_NET!r} to the sandbox service networks."
    )


def test_google_mcp_services_are_not_on_sandbox_net() -> None:
    """No Google MCP service may be on sandbox-net (issue #42).

    sandbox-net is intended as a two-member network (core + sandbox only).
    If any Google MCP sidecar were on sandbox-net the sandbox could reach it
    directly, bypassing the permission gate — the same vulnerability issue #42
    was filed to close.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    for svc in _GOOGLE_MCP_SERVICES:
        nets = _service_networks(compose, svc)
        assert _SANDBOX_NET not in nets, (
            f"{svc} is attached to {_SANDBOX_NET!r} — a sandbox bash command could "
            "reach it and act on the owner's Google account without a permission "
            f"check.  Remove {_SANDBOX_NET!r} from {svc}."
        )
