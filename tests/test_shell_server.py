"""The sandbox shell server, driven over a real socket (no /workspace dependency).

Each test points the server at a tmp workdir, starts it on an ephemeral port, and speaks
the newline-JSON protocol directly — exercising the persistent-shell behaviour core
relies on: env/cwd persistence across commands, exit-code capture, timeout, truncation.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from chief.sandbox.shell_server import ShellServer


class _Client:
    """Thin newline-JSON client kept open across commands (one task's connection)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer

    async def run(self, session_id: str, command: str) -> dict[str, object]:
        self._writer.write(
            (json.dumps({"session_id": session_id, "command": command}) + "\n").encode()
        )
        await self._writer.drain()
        line = await self._reader.readline()
        data: dict[str, object] = json.loads(line)
        return data

    async def close(self) -> None:
        self._writer.close()
        await self._writer.wait_closed()


@pytest_asyncio.fixture
async def server_port(tmp_path: Path) -> AsyncIterator[int]:
    """A running ShellServer on 127.0.0.1:<ephemeral>, workdir under tmp_path."""
    server = ShellServer(timeout=2.0, output_limit=200, workdir=str(tmp_path))
    tcp = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    port = tcp.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        await server.aclose()
        tcp.close()
        await tcp.wait_closed()


async def _client(port: int) -> _Client:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    return _Client(reader, writer)


async def test_export_persists_across_commands(server_port: int) -> None:
    client = await _client(server_port)
    await client.run("s1", "export FOO=bar")
    res = await client.run("s1", "echo $FOO")

    assert res["stdout"] == "bar"
    assert res["exit_code"] == 0
    await client.close()


async def test_cd_persists_across_commands(
    server_port: int, tmp_path: Path
) -> None:
    (tmp_path / "sub").mkdir()
    client = await _client(server_port)
    await client.run("s1", "cd sub")
    res = await client.run("s1", "pwd")

    assert res["stdout"] == str(tmp_path / "sub")
    await client.close()


async def test_exit_code_captured(server_port: int) -> None:
    client = await _client(server_port)
    # A subshell so the exit doesn't kill the long-lived shell, but $? still propagates.
    res = await client.run("s1", "(exit 5)")

    assert res["exit_code"] == 5
    # The persistent shell survives — the next command still runs.
    assert (await client.run("s1", "echo ok"))["stdout"] == "ok"
    await client.close()


async def test_nonzero_exit_code(server_port: int) -> None:
    client = await _client(server_port)
    res = await client.run("s1", "ls /no/such/path")

    assert res["exit_code"] != 0
    assert "stderr" in res and res["stderr"]
    await client.close()


async def test_stdout_and_stderr_separated(server_port: int) -> None:
    client = await _client(server_port)
    res = await client.run("s1", "echo out; echo err 1>&2")

    assert res["stdout"] == "out"
    assert res["stderr"] == "err"
    assert res["exit_code"] == 0
    await client.close()


async def test_sessions_are_isolated(server_port: int) -> None:
    c1 = await _client(server_port)
    c2 = await _client(server_port)
    await c1.run("s1", "export X=one")
    await c2.run("s2", "export X=two")

    assert (await c1.run("s1", "echo $X"))["stdout"] == "one"
    assert (await c2.run("s2", "echo $X"))["stdout"] == "two"
    await c1.close()
    await c2.close()


@pytest.mark.timeout(15)
async def test_timeout_kills_hanging_command(server_port: int) -> None:
    client = await _client(server_port)
    res = await client.run("s1", "sleep 30")

    assert res["exit_code"] == 124  # TIMEOUT_EXIT_CODE
    assert res["truncated"] is True
    # The shell respawns, so the server stays usable for the next command.
    again = await client.run("s1", "echo alive")
    assert again["stdout"] == "alive"
    await client.close()


async def test_output_truncation_flagged(server_port: int) -> None:
    client = await _client(server_port)
    # output_limit=200 in the fixture; emit well past it.
    res = await client.run("s1", "for i in $(seq 1 500); do echo line$i; done")

    assert res["truncated"] is True
    assert len(str(res["stdout"])) <= 200
    await client.close()


async def test_malformed_request_gets_error(server_port: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", server_port)
    writer.write(b"not json\n")
    await writer.drain()
    res = json.loads(await reader.readline())

    assert res["exit_code"] == 1
    assert "malformed" in res["stderr"]
    writer.close()
    await writer.wait_closed()
