"""Opt-in live test: a real Copilot turn through the real client plane (#140, of #128).

Skipped unless ``CHIEF_COPILOT_LIVE`` is set — the manual "does the whole mechanism
actually work end to end" check, not a CI test. Everything here is real: a real
``SocketServer`` on a real unix socket, a real ``CliAdapter``/``CliTaskIO`` over a real
``MessageLog``, a real ``TaskManager``, and a real ``CopilotBackend`` spawning a real
Copilot runtime. The only thing that is not the production daemon is the socket path
(a tmp dir) and the sqlite file (the ``session_factory`` fixture).

That is the point of the gate: ``tests/test_cli_platform.py`` proves the client plane
against a ``FakeSession``, and ``tests/test_copilot_backend_live.py`` proves a real
Copilot turn against the bare backend. Neither proves the two *joined* — that a real
model's stream survives the JSONL wire. This does.

Auth comes from the logged-in GitHub Copilot user — no token is passed. On the Student
plan the served model is whatever GitHub picks regardless of the name sent, so this
asserts on the *shape* of the stream (a non-empty reply frame, correctly tagged and
routed), never on the reply's wording.

Run it:

1. Log in once with the Copilot CLI so the SDK can spawn an authenticated runtime.
2. Download the pinned runtime (or let the SDK fetch it on first use)::

       uv run python -m copilot download-runtime

3. Set ``CHIEF_COPILOT_LIVE=1``, then run::

       CHIEF_COPILOT_LIVE=1 uv run pytest tests/test_cli_live.py

``CHIEF_COPILOT_MODEL`` (default ``auto``) overrides the requested model.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.cli import CliAdapter, CliTaskIO
from chief.client_plane.protocol import CLI_PLATFORM, user_frame
from chief.client_plane.server import SocketServer
from chief.core.backend import CopilotBackend
from chief.core.tasks import TaskManager
from chief.persistence.messages import MessageLog

pytestmark = pytest.mark.skipif(
    not os.environ.get("CHIEF_COPILOT_LIVE"),
    reason="live client-plane test — set CHIEF_COPILOT_LIVE=1 (needs a Copilot login)",
)

THREAD = "cli:main"


async def _never(*args: Any, **kwargs: Any) -> bool:
    """Stub the auxiliary classifiers: this test spends its live turn on the reply."""
    return False


async def _read_frame(
    reader: asyncio.StreamReader, *, timeout: float
) -> dict[str, Any]:
    line = await asyncio.wait_for(reader.readline(), timeout)
    assert line, "server closed the socket mid-stream"
    frame: dict[str, Any] = json.loads(line)
    return frame


@pytest.fixture
async def live_server(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[SocketServer]:
    """The production client plane, wired to a real ``CopilotBackend``.

    Mirrors ``app.build_engine``'s seam — ``session_factory_sdk=backend.create_session``
    — rather than the ``_seq_factory`` of fake sessions the mocked suite injects.
    """
    server = SocketServer(str(tmp_path / "chief.sock"))
    serving = asyncio.create_task(server.run())
    await asyncio.wait_for(server.started.wait(), 5)

    log = MessageLog(session_factory)
    backend = CopilotBackend()
    manager = TaskManager(
        session_factory=session_factory,
        io=CliTaskIO(server, log=log),
        owner_model=os.environ.get("CHIEF_COPILOT_MODEL", "auto"),
        classifier_model="claude-haiku-4-5",
        platform=CLI_PLATFORM,
        concurrency=1,
        turn_timeout=180.0,  # a live runtime spawn + round-trip, not a mocked one
        idle_archive_seconds=1000.0,
        compaction_idle_seconds=1000.0,
        session_factory_sdk=backend.create_session,
        stop_intent=_never,
        warrants_task=_never,
    )
    CliAdapter(
        server=server, engine=manager, log=log, session_factory=session_factory
    )
    try:
        yield server
    finally:
        # Engine first: it cancels the consumers (and closes the live Copilot session)
        # before the session_factory fixture disposes the DB engine underneath them.
        await manager.shutdown()
        await server.stop()
        serving.cancel()
        with suppress(asyncio.CancelledError):
            await serving


@pytest.mark.timeout(300)  # live runtime spawn + model round-trip; override the 30s cap
async def test_live_copilot_turn_streams_through_the_socket(
    live_server: SocketServer,
) -> None:
    """The gate: one real owner turn, end to end over the wire.

    A real client connects to the unix socket, sends a ``user`` frame, and a real
    model's answer comes back as a ``reply`` frame — tagged ``cli`` and routed to the
    thread that asked. Milestones may precede it (tool use is the model's choice, so
    their presence is not asserted); the reply is what must arrive.
    """
    reader, writer = await asyncio.open_unix_connection(live_server.path)
    try:
        hello = await _read_frame(reader, timeout=5)
        assert hello["type"] == "hello"

        writer.write(
            json.dumps(user_frame(THREAD, "Reply with exactly the word: pong")).encode()
            + b"\n"
        )
        await writer.drain()

        # Drain whatever the model chose to stream until its answer lands. A live turn
        # is slow (runtime spawn, then the round-trip), hence the generous budget.
        seen: list[dict[str, Any]] = []
        while True:
            frame = await _read_frame(reader, timeout=240)
            seen.append(frame)
            assert frame["type"] != "error", f"the turn errored on the wire: {frame!r}"
            if frame["type"] == "reply":
                break
            assert frame["type"] == "milestone", (
                f"unexpected frame before the reply: {frame!r} (saw {seen!r})"
            )

        reply = seen[-1]
        assert str(reply["text"]).strip(), f"the reply frame carried no text: {reply!r}"
        assert reply["platform"] == CLI_PLATFORM
        assert reply["thread_key"] == THREAD
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
