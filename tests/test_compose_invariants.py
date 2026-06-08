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
