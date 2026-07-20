"""MCP through the booted daemon: production wiring, real subprocess.

These boot ``build_app`` against a real stdio MCP subprocess declared by a
real package manifest — no mock stands in for the subprocess, its
environment, or the post_tool hook. Only the LLM provider is swapped, the
one scripted fake the suite allows (PRD #183).
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from chief.packages import McpServerSpec, PackageLibrary

from .fakes import FakeProvider, text_turn, tool_turn
from .test_app import boot, make_config, read_finals, send_frame, shutdown
from .test_mcp import write_server, write_slow_server

HOOK_MODULE = (
    "from chief.hooks.posttool import Annotate\n"
    "\n"
    "\n"
    "def register(context, hooks):\n"
    "    @hooks.post_tool\n"
    "    async def screen(call, result):\n"
    "        return Annotate(f'screened {call.name}')\n"
)


def install_mcp_package(
    tmp_path: Path, command: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Write a real package declaring `command` as an MCP server plus a
    post_tool hook, mark it installed, and return the packages root.

    build_app tests are isolated from the operator's install state (see
    tests/conftest.py), so the roots are supplied explicitly: packages_dir
    through Config, the registry by re-pointing the same constant conftest
    already neutralizes.
    """
    root = tmp_path / "packages"
    pkg = root / "mcpfixture"
    pkg.mkdir(parents=True)
    (pkg / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "mcpfixture",
                "description": "fixture package with a real mcp server",
                "hooks": {"module": "hooks.py", "register": "register"},
                "mcp_servers": {"testsrv": {"command": list(command)}},
            }
        )
    )
    (pkg / "hooks.py").write_text(HOOK_MODULE)
    registry = tmp_path / "installed.yaml"
    registry.write_text("mcpfixture:\n  source: bundled\n")
    monkeypatch.setattr("chief.hooks.boot.INSTALLED_REGISTRY", registry)
    return root


def supervise_tasks() -> set[asyncio.Task[Any]]:
    """Live MCP supervising tasks, read off the event loop itself rather than
    the manager's own bookkeeping (same posture as tests/test_mcp.py)."""
    return {
        task
        for task in asyncio.all_tasks()
        if "_supervise" in getattr(task.get_coro(), "__qualname__", "")
    }


async def test_booted_app_registers_a_manifest_declared_servers_tools(
    tmp_path: Path, sock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = write_server(tmp_path)
    root = install_mcp_package(tmp_path, command, monkeypatch)

    # Assert the manifest side first, so the config below is provably the
    # *same* server the package declares.
    package = PackageLibrary((root,)).get("mcpfixture")
    assert package is not None
    assert package.mcp_servers == (McpServerSpec(name="testsrv", command=command),)

    config = make_config(
        tmp_path,
        sock_path,
        packages_dir=root,
        mcp_servers={"testsrv": {"command": list(command)}},
    )
    provider = FakeProvider([text_turn("ok")])
    app, streams = await boot(config, provider)
    try:
        send_frame(streams, "hi")
        await read_finals(streams, 1)
        offered = {spec.name for spec in provider.tool_specs[0]}
        assert {
            "mcp_testsrv_add",
            "mcp_testsrv_env_var",
            "mcp_testsrv_cwd",
        } <= offered
    finally:
        await shutdown(app, streams)


async def test_declared_env_reaches_the_child_and_post_tool_screens_the_result(
    tmp_path: Path, sock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = write_server(tmp_path)
    root = install_mcp_package(tmp_path, command, monkeypatch)
    # An MCP tool is not read_only, so the gate needs it pre-approved or it
    # raises an approval card and the scripted turns desync.
    config = make_config(
        tmp_path,
        sock_path,
        packages_dir=root,
        mcp_servers={
            "testsrv": {
                "command": list(command),
                "env": {"MCP_TEST_SECRET": "s3cr3t"},
            },
        },
        gate_approved=("mcp_testsrv_env_var",),
    )
    provider = FakeProvider(
        [
            tool_turn("mcp_testsrv_env_var", {"name": "MCP_TEST_SECRET"}),
            text_turn("done"),
        ]
    )
    app, streams = await boot(config, provider)
    try:
        send_frame(streams, "hi")
        await read_finals(streams, 1)
        # One equality, two proofs: `s3cr3t` came back from os.environ inside
        # the real child process (so the configured env reached it), and the
        # fixture package's registered post_tool hook annotated the payload
        # before the model ever saw it.
        assert provider.calls[1][-1]["content"] == (
            '<hook source="mcpfixture">\nscreened mcp_testsrv_env_var\n</hook>'
            "\n\ns3cr3t"
        )
    finally:
        await shutdown(app, streams)


async def test_a_server_that_fails_to_start_costs_only_its_tools(
    tmp_path: Path, sock_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    config = make_config(
        tmp_path,
        sock_path,
        packages_dir=empty,
        mcp_servers={
            # A real command that exits immediately: connect fails fast, with
            # no timeout wait.
            "dead": {"command": [sys.executable, str(tmp_path / "missing.py")]},
            "testsrv": {"command": list(write_server(tmp_path))},
        },
    )
    provider = FakeProvider([text_turn("still here")])
    with caplog.at_level(logging.ERROR):
        app, streams = await boot(config, provider)
    try:
        assert "dead" in caplog.text
        send_frame(streams, "hi")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "still here"
        offered = {spec.name for spec in provider.tool_specs[0]}
        assert "mcp_testsrv_add" in offered
        assert not any(name.startswith("mcp_dead_") for name in offered)
    finally:
        await shutdown(app, streams)


async def test_a_timed_out_connect_leaks_no_task_and_keeps_the_daemon_up(
    tmp_path: Path, sock_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    config = make_config(
        tmp_path,
        sock_path,
        packages_dir=empty,
        mcp_servers={
            "slowsrv": {
                "command": list(write_slow_server(tmp_path)),
                "timeout": 0.3,
            },
            "testsrv": {"command": list(write_server(tmp_path))},
        },
    )
    provider = FakeProvider([text_turn("still here")])
    with caplog.at_level(logging.ERROR):
        app, streams = await boot(config, provider)
    try:
        assert "slowsrv" in caplog.text
        # One, not zero: a healthy server's supervising task holds its
        # transport open by design. A leaked slowsrv supervisor — which
        # McpManager.stop could no longer cancel — would make it 2.
        assert len(supervise_tasks()) == 1
        send_frame(streams, "hi")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "still here"
        offered = {spec.name for spec in provider.tool_specs[0]}
        assert "mcp_testsrv_add" in offered
        assert not any(name.startswith("mcp_slowsrv_") for name in offered)
    finally:
        await shutdown(app, streams)
