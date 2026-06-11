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
# After the issue #52 cutover there is a single Gmail service (mcp-gmail) running the
# chief-owned image; the transitional mcp-gmail-chief service was removed.
_GOOGLE_MCP_SERVICES = {
    "mcp-calendar",
    "mcp-drive",
    "mcp-sheets",
    "mcp-gmail",
}


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


def test_mcp_playwright_healthcheck_probes_the_served_mcp_endpoint() -> None:
    """The mcp-playwright healthcheck must hit the served /mcp endpoint, not /.

    @playwright/mcp mounts its streamable-HTTP handler at /mcp; a GET to the
    root path returns 404, so a `curl -f http://127.0.0.1:3000/` health probe
    fails on every check and the container is flagged unhealthy even though the
    server is up (issue #38 prod rollout). The probe must target /mcp and must
    not use curl's --fail/-f (the endpoint answers a bare GET with a non-2xx
    status — a liveness probe only needs the server to respond, not to 200).
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    test_cmd = compose["services"]["mcp-playwright"]["healthcheck"]["test"]
    assert isinstance(test_cmd, list)
    joined = " ".join(test_cmd)
    assert "/mcp" in joined, (
        "mcp-playwright healthcheck must probe the served /mcp endpoint, not the "
        f"root path (which 404s). Got: {joined!r}"
    )
    fail_flags = {"-f", "-fsS", "-sf", "--fail"}
    assert not fail_flags.intersection(test_cmd), (
        "mcp-playwright healthcheck must not use curl --fail/-f: the /mcp endpoint "
        "answers a bare GET with a non-2xx status, so -f flags a live server "
        f"unhealthy. Got: {joined!r}"
    )


def test_playwright_dockerfile_allows_the_cross_container_host() -> None:
    """The mcp-playwright Dockerfile CMD must allowlist core's Host header.

    @playwright/mcp 0.0.76 has a DNS-rebinding guard: it 403s any request whose
    Host header isn't in --allowed-hosts (defaulting to the bound host, which it
    reports as localhost:3000). Core reaches the server at
    http://mcp-playwright:3000/mcp, so its Host header is 'mcp-playwright:3000' —
    without that host in the allowlist the MCP handshake gets
    'Access is only allowed at localhost:3000' (403), the browser tools never load
    into the owner agent, and every browser call fails. --host 0.0.0.0 only widens
    the *bind*, not the Host-header check; --allowed-hosts is the lever that widens
    the check (prod rollout: browser dead since launch).
    """
    dockerfile = (
        Path(__file__).parent.parent / "docker" / "mcp-playwright" / "Dockerfile"
    )
    content = dockerfile.read_text()
    assert "--allowed-hosts" in content, (
        "docker/mcp-playwright/Dockerfile CMD does not include --allowed-hosts. "
        "@playwright/mcp 0.0.76 403s requests whose Host header isn't allowlisted; "
        "core reaches the server as 'mcp-playwright:3000'. Add '--allowed-hosts', "
        "'mcp-playwright:3000' (the cross-container Host) to the CMD."
    )
    assert "mcp-playwright:3000" in content, (
        "docker/mcp-playwright/Dockerfile --allowed-hosts must include "
        "'mcp-playwright:3000' — that is the Host header core sends "
        "(http://mcp-playwright:3000/mcp). Without it the MCP handshake 403s and "
        "the browser tools never load."
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


# ---- google_token directory mount in core (issue #54, RW per issue #53) --------

#: The container path where the token *directory* must be mounted in core.
#: build_list_accounts_service() defaults to Path('/token') as the discovery
#: directory; the add-account flow (issue #53) writes newly-minted
#: google_token_<slug>.json files here so they are re-scanned without a restart.
_CORE_TOKEN_DIR_MOUNT = "/token"
#: The canonical host-side source of the token directory mount (issue #58).
#: ALL token consumers resolve to this subdirectory — never the flat
#: secrets/google_token.json path that predated issue #57.
_HOST_TOKEN_DIR = "./secrets/google_tokens"
#: Old (pre-#58) flat path — must not appear in any token mount after this fix.
_LEGACY_HOST_TOKEN = "./secrets/google_token.json"


def _core_token_dir_entry(compose: dict[str, Any]) -> str | None:
    """Return core's volume entry whose target is exactly /token, else None.

    Matches the directory mount (source:/token[:mode]) but not the
    google_oauth_client.json file mount (source:/token/google_oauth_client.json).
    """
    core_volumes: list[str] = compose["services"]["core"].get("volumes", [])
    for v in core_volumes:
        if not isinstance(v, str):
            continue
        parts = v.split(":")
        # source:target  or  source:target:mode
        if len(parts) >= 2 and parts[1] == _CORE_TOKEN_DIR_MOUNT:
            return v
    return None


def test_google_token_mounted_in_core() -> None:
    """core must have the token directory bind-mounted at /token.

    build_list_accounts_service() defaults to Path('/token') as the discovery
    directory.  Without the directory mount, core's /token is absent or empty
    and discover_accounts() returns [] — zero accounts on a live deployment
    (issue #54 Defect 1).  A directory (not single-file) mount is required so
    every google_token*.json — including accounts added at runtime (issue #53)
    — is visible.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    entry = _core_token_dir_entry(compose)
    assert entry is not None, (
        f"core service is missing a bind-mount of the token directory at "
        f"{_CORE_TOKEN_DIR_MOUNT!r}. Add "
        f"'- {_HOST_TOKEN_DIR}:{_CORE_TOKEN_DIR_MOUNT}' to core.volumes so "
        "discover_accounts() can find every token at /token."
    )
    assert entry.startswith(_HOST_TOKEN_DIR + ":"), (
        f"core's /token mount ({entry!r}) must be sourced from "
        f"{_HOST_TOKEN_DIR!r} (issue #58 canonical path)."
    )


def test_google_token_mount_in_core_is_writable() -> None:
    """core's /token directory mount must be writable (NOT :ro) — issue #53.

    The runtime add-account flow mints a new google_token_<email-slug>.json into
    /token from inside core, so discover_accounts() picks it up with no restart.
    core writes only *new* per-account files and never the shared
    google_token.json, so it does not contend with mcp-sheets (which solely
    refreshes existing account files); the writes target distinct files.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    entry = _core_token_dir_entry(compose)
    assert entry is not None, (
        f"No volume entry targeting {_CORE_TOKEN_DIR_MOUNT!r} found in core.volumes."
    )
    assert not entry.endswith(":ro"), (
        f"core's /token mount ({entry!r}) must be read-write so the add-account "
        "flow (issue #53) can mint new google_token_<slug>.json files there. "
        "Remove the ':ro' suffix."
    )


# ---- mcp-calendar token directory mount (issue #56) --------------------------

#: The container path where the token directory must be mounted in mcp-calendar.
_CALENDAR_TOKEN_DIR = "/token"
#: The env var that tells server.py which directory to scan for google_token*.json.
_CALENDAR_TOKEN_DIR_ENV = "TOKEN_DIR"


def test_mcp_calendar_token_dir_mounted() -> None:
    """mcp-calendar must have a directory bind-mounted at /token.

    Mounting only a single file (google_token.json) prevents additional
    google_token_<label>.json files from reaching the container — making
    multi-account calendar completely non-functional in a real deploy even after
    the in-process contextvar fix (issue #56).  The mount must cover the whole
    token directory so every google_token*.json is visible to the server.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    cal_volumes: list[str] = compose["services"]["mcp-calendar"].get("volumes", [])
    # Accept either a directory mount (./secrets:/token) or any entry that maps
    # something to exactly /token (without a filename suffix).
    has_dir_mount = any(
        isinstance(v, str) and (
            v.split(":")[1].rstrip(":ro").rstrip(":rw") == _CALENDAR_TOKEN_DIR
            if len(v.split(":")) >= 2 else False
        )
        for v in cal_volumes
    )
    assert has_dir_mount, (
        f"mcp-calendar is missing a directory bind-mount at {_CALENDAR_TOKEN_DIR!r}. "
        "Add '- ./secrets:/token:ro' (or a subdirectory) so that all "
        "google_token*.json files reach the container for multi-account support "
        "(issue #56).  A single-file mount blocks the second account's token."
    )


def test_mcp_calendar_token_dir_is_read_only() -> None:
    """The token directory mount in mcp-calendar must be read-only (:ro).

    mcp-sheets is the sole writer of the shared token files.  A writable mount
    in mcp-calendar would risk concurrent writes and token corruption.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    cal_volumes: list[str] = compose["services"]["mcp-calendar"].get("volumes", [])
    token_entry = next(
        (
            v
            for v in cal_volumes
            if isinstance(v, str)
            and len(v.split(":")) >= 2
            and v.split(":")[1].rstrip(":ro").rstrip(":rw") == _CALENDAR_TOKEN_DIR
        ),
        None,
    )
    assert token_entry is not None, (
        f"No volume entry mounting {_CALENDAR_TOKEN_DIR!r} found in "
        "mcp-calendar.volumes."
    )
    assert token_entry.endswith(":ro"), (
        f"The token dir mount in mcp-calendar ({token_entry!r}) must end with ':ro'. "
        "mcp-sheets is the sole writer; a writable mount risks token corruption."
    )


def test_mcp_calendar_token_dir_env_set() -> None:
    """mcp-calendar must set TOKEN_DIR so server.py scans the mounted directory.

    Without TOKEN_DIR the server falls back to TOKEN_PATH's parent, which may
    not match the actual mount point.  An explicit TOKEN_DIR=/token makes the
    scan path unambiguous and independent of GOOGLE_TOKEN_PATH.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    env = compose["services"]["mcp-calendar"].get("environment", {}) or {}
    if isinstance(env, list):
        env_dict: dict[str, str] = {}
        for entry in env:
            if "=" in entry:
                k, _, v = entry.partition("=")
                env_dict[k] = v
        env = env_dict
    assert _CALENDAR_TOKEN_DIR_ENV in env, (
        f"mcp-calendar environment is missing {_CALENDAR_TOKEN_DIR_ENV!r}. "
        f"Add '{_CALENDAR_TOKEN_DIR_ENV}: {_CALENDAR_TOKEN_DIR}' so server.py "
        "scans the mounted token directory for all google_token*.json files."
    )
    assert env[_CALENDAR_TOKEN_DIR_ENV] == _CALENDAR_TOKEN_DIR, (
        f"mcp-calendar {_CALENDAR_TOKEN_DIR_ENV}={env[_CALENDAR_TOKEN_DIR_ENV]!r} "
        f"should be {_CALENDAR_TOKEN_DIR!r}."
    )


# ---- mcp-calendar token mount security (issue #57) ---------------------------

#: The only permitted host-side source for the mcp-calendar /token mount.
#: Must be the dedicated google_tokens subdir — never the whole secrets dir.
_CALENDAR_TOKEN_HOST_SOURCE = "./secrets/google_tokens"
#: Whole-secrets-dir path that must NOT be the source (the pre-fix value).
_SECRETS_DIR = "./secrets"


def _calendar_token_entry(compose: dict[str, Any]) -> str | None:
    """Return the volume entry that mounts /token in mcp-calendar, or None."""
    cal_volumes: list[str] = compose["services"]["mcp-calendar"].get("volumes", [])
    return next(
        (
            v
            for v in cal_volumes
            if isinstance(v, str)
            and len(v.split(":")) >= 2
            and v.split(":")[1] == _CALENDAR_TOKEN_DIR
        ),
        None,
    )


def test_mcp_calendar_token_mount_uses_dedicated_subdir() -> None:
    """mcp-calendar /token must be mounted from ./secrets/google_tokens, not ./secrets.

    Mounting the whole ./secrets directory exposes unrelated secrets
    (claude_code_oauth_token, discord_bot_token, google_oauth_client.json,
    telegram_bot_token) inside the calendar container.  A compromise of the
    calendar container or its supply chain would leak those secrets.  Only the
    dedicated google_tokens subdirectory — containing solely google_token*.json
    files — may be mounted at /token (issue #57).
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    entry = _calendar_token_entry(compose)
    assert entry is not None, (
        f"mcp-calendar has no volume entry mounting {_CALENDAR_TOKEN_DIR!r}. "
        f"Add '- {_CALENDAR_TOKEN_HOST_SOURCE}:{_CALENDAR_TOKEN_DIR}:ro'."
    )
    host_source = entry.split(":")[0]
    assert host_source != _SECRETS_DIR, (
        f"mcp-calendar mounts the entire secrets directory ({_SECRETS_DIR!r}) at "
        f"{_CALENDAR_TOKEN_DIR!r}.  This exposes claude_code_oauth_token, "
        "discord_bot_token, google_oauth_client.json and telegram_bot_token inside "
        "the container.  Change the host source to the dedicated tokens subdir: "
        f"'- {_CALENDAR_TOKEN_HOST_SOURCE}:{_CALENDAR_TOKEN_DIR}:ro' (issue #57)."
    )
    assert host_source == _CALENDAR_TOKEN_HOST_SOURCE, (
        f"mcp-calendar /token host source is {host_source!r}; "
        f"expected {_CALENDAR_TOKEN_HOST_SOURCE!r}. "
        "Only the dedicated google_tokens subdir should be mounted."
    )


def test_mcp_calendar_token_mount_is_read_only_subdir() -> None:
    """The dedicated tokens subdir mount in mcp-calendar must be :ro.

    This is a belt-and-suspenders check: the subdir-source assertion above
    ensures the right directory, this one ensures the :ro flag is preserved
    after the source change (issue #57).
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    entry = _calendar_token_entry(compose)
    assert entry is not None, (
        f"mcp-calendar has no volume entry mounting {_CALENDAR_TOKEN_DIR!r}."
    )
    assert entry.endswith(":ro"), (
        f"mcp-calendar token mount ({entry!r}) must end with ':ro'. "
        "Even with the narrowed subdir source, the mount must be read-only."
    )


# ---- unified google token path invariants (issue #58) -------------------------
#
# ALL five token-consuming services (core, mcp-calendar, mcp-drive, mcp-sheets,
# mcp-gmail) must source their token from ./secrets/google_tokens/ — the single
# canonical subdirectory.  The legacy flat path (./secrets/google_token.json) must
# not appear anywhere in a token-related bind-mount after this fix.
#
# mcp-calendar mounts the whole directory (multi-account scan); the other four
# mount the single file google_token.json inside that directory.  Either way the
# host-side source resolves inside ./secrets/google_tokens/ — never directly
# under ./secrets/.


def _token_volumes(compose: dict[str, Any], service: str) -> list[str]:
    """Return bind-mount volume entries for *service* that reference a token path."""
    vols: list[str] = compose["services"][service].get("volumes", [])
    return [v for v in vols if isinstance(v, str) and "google_token" in v]


def _host_source(entry: str) -> str:
    """Extract the host-side path from a 'host:container[:mode]' volume entry."""
    return entry.split(":")[0]


def test_no_service_mounts_legacy_flat_token() -> None:
    """No token-consuming service may mount the legacy ./secrets/google_token.json.

    After issue #58 the single canonical location is ./secrets/google_tokens/.
    A service that still mounts the old flat path would diverge from the shared
    token, breaking refresh-token rotation consistency (mcp-sheets rotates the
    file in google_tokens/; a flat-path consumer would read a stale token).
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    for svc in (
        "core",
        "mcp-drive",
        "mcp-sheets",
        "mcp-gmail",
        "mcp-calendar",
    ):
        for entry in _token_volumes(compose, svc):
            src = _host_source(entry)
            assert src != _LEGACY_HOST_TOKEN, (
                f"{svc} still mounts the legacy flat token path "
                f"({_LEGACY_HOST_TOKEN!r}).  Change it to source from "
                f"{_HOST_TOKEN_DIR!r} so all services share the same "
                "canonical token location (issue #58)."
            )


def test_all_token_consumers_source_from_google_tokens_subdir() -> None:
    """All token-consuming services must source tokens from ./secrets/google_tokens/.

    core, mcp-drive, and mcp-sheets mount the single file
    ./secrets/google_tokens/google_token.json; mcp-calendar and mcp-gmail (chief-owned
    after the issue #52 cutover) mount the directory ./secrets/google_tokens.  In either
    case the host-side source must start with ./secrets/google_tokens — never the whole
    ./secrets dir or the legacy flat path (issue #58).
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    for svc in (
        "core",
        "mcp-drive",
        "mcp-sheets",
        "mcp-gmail",
        "mcp-calendar",
    ):
        token_vols = _token_volumes(compose, svc)
        assert token_vols, (
            f"{svc} has no volume entry referencing a google_token path. "
            "It must mount its token from ./secrets/google_tokens/."
        )
        for entry in token_vols:
            src = _host_source(entry)
            assert src.startswith(_HOST_TOKEN_DIR), (
                f"{svc} mounts a token from {src!r}, which is outside the "
                f"canonical {_HOST_TOKEN_DIR!r} subdirectory.  All six "
                "token-consuming services must share a single host path so "
                "mcp-sheets rotation stays consistent (issue #58)."
            )


def test_mcp_sheets_token_mount_is_writable() -> None:
    """mcp-sheets token mount must be writable (no :ro suffix).

    mcp-sheets is the sole writer of the shared token — it persists the
    refreshed credentials on every Google API call.  A read-only mount
    would prevent the write and leave the refresh token stale.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    token_vols = _token_volumes(compose, "mcp-sheets")
    assert token_vols, "mcp-sheets has no google_token volume entry."
    entry = token_vols[0]
    assert not entry.endswith(":ro"), (
        f"mcp-sheets token mount ({entry!r}) must not be read-only. "
        "mcp-sheets is the sole writer; it must be able to persist "
        "refreshed credentials (issue #58)."
    )


def test_mcp_sheets_and_mcp_calendar_share_same_host_token_dir() -> None:
    """mcp-sheets (writer) and mcp-calendar (scanner) must share the same host dir.

    mcp-sheets persists refresh-token rotations; mcp-calendar scans that directory
    for all google_token*.json files.  If they point at different host paths the
    calendar container reads a stale token after every rotation (issue #58).

    After issue #47 mcp-sheets mounts the full directory (like mcp-calendar) so
    it can write per-account token files atomically.  Both entries must therefore
    resolve to the same canonical host directory.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    sheets_vols = _token_volumes(compose, "mcp-sheets")
    cal_entry = _calendar_token_entry(compose)
    assert sheets_vols, "mcp-sheets has no google_token volume entry."
    assert cal_entry is not None, "mcp-calendar has no /token volume entry."
    sheets_src = _host_source(sheets_vols[0])
    cal_src = _host_source(cal_entry)
    # Normalise both sources to their directory component so we can compare
    # even when one mounts the dir and the other mounts a file inside it.
    # A path that already IS the canonical dir has no trailing filename; a
    # path that ends with ``/google_token*.json`` needs the filename stripped.
    def _to_dir(src: str) -> str:
        # If the source ends with a google_token*.json filename, strip it.
        import re as _re
        return _re.sub(r"/google_token[^/]*$", "", src) or src

    sheets_dir = _to_dir(sheets_src)
    cal_dir = _to_dir(cal_src)
    assert sheets_dir == cal_dir, (
        f"mcp-sheets sources its token from {sheets_src!r} (dir: {sheets_dir!r}) "
        f"but mcp-calendar mounts {cal_src!r} (dir: {cal_dir!r}).  They must "
        "share the same host dir for rotation consistency (issue #58)."
    )


# ---- mcp-drive and mcp-sheets directory mounts (issue #47) -------------------
#
# After issue #47, mcp-drive and mcp-sheets both scan a token directory (like
# mcp-calendar) so they can support multiple accounts.  Both must mount the full
# ./secrets/google_tokens directory at /token, not just the single primary file.


_DRIVE_TOKEN_DIR = "/token"
_SHEETS_TOKEN_DIR = "/token"
_DRIVE_TOKEN_DIR_ENV = "TOKEN_DIR"
_SHEETS_TOKEN_DIR_ENV = "TOKEN_DIR"


def _has_dir_mount_at(volumes: list[str], target_dir: str) -> bool:
    """Return True if any volume mounts something to exactly *target_dir*."""
    for v in volumes:
        if not isinstance(v, str):
            continue
        parts = v.split(":")
        if len(parts) < 2:
            continue
        # Strip a trailing :ro / :rw mode from the target component.
        mount_target = parts[1].rstrip("/")
        if mount_target == target_dir:
            return True
    return False


def test_mcp_drive_token_dir_mounted() -> None:
    """mcp-drive must have a directory bind-mounted at /token (issue #47).

    A single-file mount prevents additional google_token_<label>.json files
    from reaching the container, making multi-account drive non-functional.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    drive_volumes: list[str] = compose["services"]["mcp-drive"].get("volumes", [])
    assert _has_dir_mount_at(drive_volumes, _DRIVE_TOKEN_DIR), (
        f"mcp-drive is missing a directory bind-mount at {_DRIVE_TOKEN_DIR!r}. "
        "Add '- ./secrets/google_tokens:/token:ro' so all google_token*.json "
        "files reach the container for multi-account support (issue #47)."
    )


def test_mcp_drive_token_dir_is_read_only() -> None:
    """The token directory mount in mcp-drive must be read-only (:ro).

    mcp-sheets is the sole writer of all token files.  A writable drive mount
    would risk concurrent writes and token corruption.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    drive_volumes: list[str] = compose["services"]["mcp-drive"].get("volumes", [])
    token_entry = next(
        (
            v
            for v in drive_volumes
            if isinstance(v, str) and len(v.split(":")) >= 2
            and v.split(":")[1] == _DRIVE_TOKEN_DIR
        ),
        None,
    )
    assert token_entry is not None, (
        f"No volume entry mounting {_DRIVE_TOKEN_DIR!r} found in mcp-drive.volumes."
    )
    assert token_entry.endswith(":ro"), (
        f"The token dir mount in mcp-drive ({token_entry!r}) must end with ':ro'. "
        "mcp-sheets is the sole writer; a writable drive mount risks token corruption."
    )


def test_mcp_drive_token_dir_env_set() -> None:
    """mcp-drive must set TOKEN_DIR so server.py scans the mounted dir (issue #47)."""
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    env = compose["services"]["mcp-drive"].get("environment", {}) or {}
    if isinstance(env, list):
        env_dict: dict[str, str] = {}
        for entry in env:
            if "=" in entry:
                k, _, v = entry.partition("=")
                env_dict[k] = v
        env = env_dict
    assert _DRIVE_TOKEN_DIR_ENV in env, (
        f"mcp-drive environment is missing {_DRIVE_TOKEN_DIR_ENV!r}. "
        f"Add '{_DRIVE_TOKEN_DIR_ENV}: {_DRIVE_TOKEN_DIR}' so server.py "
        "scans the mounted token directory for all google_token*.json files."
    )
    assert env[_DRIVE_TOKEN_DIR_ENV] == _DRIVE_TOKEN_DIR, (
        f"mcp-drive {_DRIVE_TOKEN_DIR_ENV}={env[_DRIVE_TOKEN_DIR_ENV]!r} "
        f"should be {_DRIVE_TOKEN_DIR!r}."
    )


def test_mcp_sheets_token_dir_mounted() -> None:
    """mcp-sheets must have a directory bind-mounted at /token (issue #47).

    A single-file mount prevents per-account token write-back to separate files
    — the multi-account write-race fix requires all google_token*.json files to
    be reachable by the sheets server.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    sheets_volumes: list[str] = compose["services"]["mcp-sheets"].get("volumes", [])
    assert _has_dir_mount_at(sheets_volumes, _SHEETS_TOKEN_DIR), (
        f"mcp-sheets is missing a directory bind-mount at {_SHEETS_TOKEN_DIR!r}. "
        "Add '- ./secrets/google_tokens:/token' so all google_token*.json "
        "files are writable for per-account token persistence (issue #47)."
    )


def test_mcp_sheets_token_dir_env_set() -> None:
    """mcp-sheets must set TOKEN_DIR so server.py scans the mounted dir (issue #47)."""
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    env = compose["services"]["mcp-sheets"].get("environment", {}) or {}
    if isinstance(env, list):
        env_dict2: dict[str, str] = {}
        for entry in env:
            if "=" in entry:
                k, _, v = entry.partition("=")
                env_dict2[k] = v
        env = env_dict2
    assert _SHEETS_TOKEN_DIR_ENV in env, (
        f"mcp-sheets environment is missing {_SHEETS_TOKEN_DIR_ENV!r}. "
        f"Add '{_SHEETS_TOKEN_DIR_ENV}: {_SHEETS_TOKEN_DIR}' so server.py "
        "scans the mounted token directory for all google_token*.json files."
    )
    assert env[_SHEETS_TOKEN_DIR_ENV] == _SHEETS_TOKEN_DIR, (
        f"mcp-sheets {_SHEETS_TOKEN_DIR_ENV}={env[_SHEETS_TOKEN_DIR_ENV]!r} "
        f"should be {_SHEETS_TOKEN_DIR!r}."
    )


# ---- mcp-gmail chief-owned token directory mount (issue #48; cutover #52) -----
#
# After the issue #52 cutover, the mcp-gmail service runs the chief-owned image
# (docker/mcp-gmail-chief) and the transitional mcp-gmail-chief service is gone.
# Like mcp-calendar, it scans the whole token DIRECTORY at /token for multi-account
# support — not just the single legacy file.

#: The container path where the token directory must be mounted in mcp-gmail.
_GMAIL_CHIEF_TOKEN_DIR = "/token"
#: The only permitted host-side source for the mcp-gmail /token mount.
_GMAIL_CHIEF_TOKEN_HOST_SOURCE = "./secrets/google_tokens"


def _gmail_chief_token_entry(compose: dict[str, Any]) -> str | None:
    """Return the volume entry that mounts /token in mcp-gmail, or None."""
    vols: list[str] = compose["services"]["mcp-gmail"].get("volumes", [])
    return next(
        (
            v
            for v in vols
            if isinstance(v, str)
            and len(v.split(":")) >= 2
            and v.split(":")[1] == _GMAIL_CHIEF_TOKEN_DIR
        ),
        None,
    )


def test_mcp_gmail_token_dir_mounted() -> None:
    """mcp-gmail (chief-owned) must have a directory bind-mounted at /token.

    After the issue #52 cutover, mcp-gmail runs the chief-owned multi-account server.
    Mounting only a single file prevents additional google_token_<label>.json files
    from reaching the container — making multi-account Gmail non-functional.  The
    mount must cover the whole token directory so every google_token*.json is visible.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    has_dir_mount = _gmail_chief_token_entry(compose) is not None
    assert has_dir_mount, (
        f"mcp-gmail is missing a directory bind-mount at "
        f"{_GMAIL_CHIEF_TOKEN_DIR!r}. "
        f"Add '- {_GMAIL_CHIEF_TOKEN_HOST_SOURCE}:{_GMAIL_CHIEF_TOKEN_DIR}:ro'"
        " so all google_token*.json files reach the container."
    )


def test_mcp_gmail_token_dir_is_read_only() -> None:
    """The token directory mount in mcp-gmail must be read-only (:ro).

    mcp-sheets is the sole writer of the shared token files.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    entry = _gmail_chief_token_entry(compose)
    assert entry is not None, (
        f"No volume entry mounting {_GMAIL_CHIEF_TOKEN_DIR!r} found in "
        "mcp-gmail.volumes."
    )
    assert entry.endswith(":ro"), (
        f"The token dir mount in mcp-gmail ({entry!r}) must end with ':ro'. "
        "mcp-sheets is the sole writer; a writable mount risks token corruption."
    )


def test_mcp_gmail_token_mount_uses_dedicated_subdir() -> None:
    """mcp-gmail /token must be from ./secrets/google_tokens, not ./secrets.

    Mounting the whole ./secrets directory exposes unrelated secrets inside the
    container.  Only the dedicated google_tokens subdirectory may be mounted at /token
    (mirrors issue #57 for calendar).
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    entry = _gmail_chief_token_entry(compose)
    assert entry is not None, (
        f"mcp-gmail has no volume entry mounting {_GMAIL_CHIEF_TOKEN_DIR!r}. "
        f"Add '- {_GMAIL_CHIEF_TOKEN_HOST_SOURCE}:{_GMAIL_CHIEF_TOKEN_DIR}:ro'."
    )
    host_source = entry.split(":")[0]
    assert host_source != _SECRETS_DIR, (
        f"mcp-gmail mounts the entire secrets directory ({_SECRETS_DIR!r}) at "
        f"{_GMAIL_CHIEF_TOKEN_DIR!r}.  Change the host source to the dedicated subdir: "
        f"'- {_GMAIL_CHIEF_TOKEN_HOST_SOURCE}:{_GMAIL_CHIEF_TOKEN_DIR}:ro'."
    )
    assert host_source == _GMAIL_CHIEF_TOKEN_HOST_SOURCE, (
        f"mcp-gmail /token host source is {host_source!r}; "
        f"expected {_GMAIL_CHIEF_TOKEN_HOST_SOURCE!r}."
    )


def test_mcp_gmail_token_dir_env_set() -> None:
    """mcp-gmail must set TOKEN_DIR so server.py scans the mounted directory."""
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    env = compose["services"]["mcp-gmail"].get("environment", {}) or {}
    if isinstance(env, list):
        env_dict: dict[str, str] = {}
        for entry in env:
            if "=" in entry:
                k, _, v = entry.partition("=")
                env_dict[k] = v
        env = env_dict
    assert "TOKEN_DIR" in env, (
        "mcp-gmail environment is missing 'TOKEN_DIR'. "
        "Add 'TOKEN_DIR: /token' so server.py scans the mounted token directory."
    )
    assert env["TOKEN_DIR"] == _GMAIL_CHIEF_TOKEN_DIR, (
        f"mcp-gmail TOKEN_DIR={env['TOKEN_DIR']!r} "
        f"should be {_GMAIL_CHIEF_TOKEN_DIR!r}."
    )


def test_mcp_gmail_runs_chief_owned_image() -> None:
    """mcp-gmail must build from docker/mcp-gmail-chief after the issue #52 cutover.

    The third-party mcp-google-gmail dependency was dropped; mcp-gmail now runs chief's
    own FastMCP server image.  Asserting the build context guards against a regression
    that re-points the service at the removed wrapper.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    build = compose["services"]["mcp-gmail"].get("build", {})
    context = build.get("context") if isinstance(build, dict) else build
    assert context == "./docker/mcp-gmail-chief", (
        f"mcp-gmail build context is {context!r}; after the issue #52 cutover it must "
        "build from './docker/mcp-gmail-chief' (the chief-owned server). The "
        "third-party mcp-google-gmail wrapper was dropped."
    )


def test_no_transitional_gmail_chief_service() -> None:
    """The transitional mcp-gmail-chief service must be gone after the cutover.

    Issue #52 consolidated the two Gmail services into one (mcp-gmail running the
    chief-owned image).  A lingering mcp-gmail-chief service would mean the cutover
    was only half-applied.
    """
    compose = yaml.safe_load(_COMPOSE_PATH.read_text())
    assert "mcp-gmail-chief" not in compose["services"], (
        "mcp-gmail-chief still exists in docker-compose.yml. The issue #52 cutover "
        "folds it into the mcp-gmail service — remove the transitional service."
    )
