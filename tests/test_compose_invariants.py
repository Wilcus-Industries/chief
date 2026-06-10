"""Regression tests for docker-compose.yml invariants (issue #16).

These tests parse the compose file directly so CI catches config regressions
without needing Docker. They cover the read-only-rootfs writable-path contract
that the smoke test cannot exercise (the smoke test crashes on missing secrets
before any claude CLI invocation).
"""

from pathlib import Path

import yaml  # type: ignore[import-untyped]

_COMPOSE_PATH = Path(__file__).parent.parent / "docker-compose.yml"


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
