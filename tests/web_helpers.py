"""Shared builder for the web-UI integration suites (#153).

Builds the WHOLE real stack the web app fronts: a real bound :class:`SocketServer`, a
real ``platform="cli"`` :class:`TaskManager` (LLM stubbed at the established
fake-session seam), a real :class:`CliAdapter`, a real :class:`SocketBridge` attached
over the unix socket, and the real app served by the real embedded
:class:`~chief.web.server.WebServer` on an ephemeral localhost port — httpx drives it
over actual TCP (httpx's ASGITransport buffers whole responses, which would deadlock
an SSE stream; real HTTP streams). The HTTP layer is never mocked. Mirrors
``tests/test_cli_platform.py`` one level up the stack.
"""

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

from chief.adapters.cli import CLI_LIMIT, CliAdapter, CliTaskIO, ForeignPlatform
from chief.client_plane import SocketServer
from chief.gate.approvals import ApprovalManager, ApprovalRegistry
from chief.gate.blacklist import Blacklist
from chief.gate.policy import PolicyStore
from chief.obs.audit import AuditLog
from chief.persistence.messages import MessageLog
from chief.web.app import WebDeps, build_web_app
from chief.web.auth import WebAuth
from chief.web.bridge import SocketBridge
from chief.web.server import WebServer
from test_cli_platform import _cli_manager, _running_server

PASSWORD = "correct-horse-battery"


@dataclass
class WebStack:
    """Handles for one running web-over-socket stack, plus its teardown."""

    server: SocketServer
    server_task: asyncio.Task[None]
    manager: Any
    bridge: SocketBridge
    auth: WebAuth
    app: Starlette
    web: WebServer
    web_task: asyncio.Task[None]
    client: httpx.AsyncClient
    registry: ApprovalRegistry | None = None
    extra_managers: list[Any] = field(default_factory=list)

    async def aclose(self) -> None:
        await self.client.aclose()
        await self.web.stop()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(self.web_task, 10)
        await self.bridge.stop()
        # Engine first — its consumers must stop touching the DB before the
        # session_factory fixture disposes its engine (see test_cli_platform).
        for manager in [self.manager, *self.extra_managers]:
            await manager.shutdown()
        await self.server.stop()
        self.server_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.server_task


async def start_web_stack(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    sdk_factory: Any,
    with_gate: bool = False,
    foreign: dict[str, ForeignPlatform] | None = None,
    deps_extra: dict[str, Any] | None = None,
) -> WebStack:
    """Bring the full stack up and return a logged-in HTTP client over it.

    ``with_gate`` wires a real PolicyStore/ApprovalManager/Blacklist into the engine
    (the ``gate_cli_stack`` shape) so approval cards flow for real; otherwise the
    plain ``_cli_manager`` engine runs.
    """
    server, server_task = await _running_server(str(tmp_path / "web.sock"))
    log = MessageLog(session_factory)
    io = CliTaskIO(server, log=log)
    registry: ApprovalRegistry | None = None
    if with_gate:
        from chief.core.tasks import TaskManager
        from test_cli_platform import _no

        registry = ApprovalRegistry()
        audit = AuditLog(str(tmp_path / "web-audit.jsonl"))
        policy = PolicyStore(session_factory, audit=audit)
        await policy.load()
        approvals = ApprovalManager(
            session_factory=session_factory,
            io=io,
            policy=policy,
            audit=audit,
            registry=registry,
            timeout_seconds=5.0,
        )
        manager = TaskManager(
            session_factory=session_factory,
            io=io,
            owner_model="claude-sonnet-4-6",
            classifier_model="claude-haiku-4-5",
            platform="cli",
            concurrency=3,
            turn_timeout=1000.0,
            idle_archive_seconds=1000.0,
            compaction_idle_seconds=1000.0,
            message_limit=CLI_LIMIT,
            session_factory_sdk=sdk_factory,
            stop_intent=_no,
            warrants_task=_no,
            policy=policy,
            approvals=approvals,
            audit=audit,
            blacklist=Blacklist.from_config(),
        )
    else:
        manager = _cli_manager(session_factory, io, factory=sdk_factory)
    CliAdapter(
        server=server,
        engine=manager,
        log=log,
        approvals=registry,
        session_factory=session_factory,
        foreign=foreign,
    )

    bridge = SocketBridge(server.path)
    await bridge.start()

    auth = WebAuth(tmp_path / "web-secrets")
    auth.set_password(PASSWORD)
    app = build_web_app(WebDeps(auth=auth, bridge=bridge, **(deps_extra or {})))
    web = WebServer(app, host="127.0.0.1", port=0)
    web_task = asyncio.create_task(web.run())
    await asyncio.wait_for(web.started.wait(), 10)
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{web.bound_port}")
    resp = await client.post("/login", data={"password": PASSWORD})
    assert resp.status_code == 303, "login must succeed before the test drives HTTP"

    return WebStack(
        server=server,
        server_task=server_task,
        manager=manager,
        bridge=bridge,
        auth=auth,
        app=app,
        web=web,
        web_task=web_task,
        client=client,
        registry=registry,
    )


class SseReader:
    """Read named SSE events off a live ``/events`` stream, with timeouts."""

    def __init__(
        self, client: httpx.AsyncClient, url: str, *, timeout: float = 5.0
    ) -> None:
        self._client = client
        self._url = url
        self._timeout = timeout
        self._cm: Any = None
        self._lines: Any = None

    async def __aenter__(self) -> "SseReader":
        self._cm = self._client.stream(
            "GET", self._url, timeout=httpx.Timeout(5.0, read=30.0)
        )
        response = await self._cm.__aenter__()
        assert response.status_code == 200
        self._lines = response.aiter_lines().__aiter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._cm.__aexit__(*exc)

    async def next_event(self) -> tuple[str, str]:
        """The next ``(event, data)`` pair; data lines re-joined with newlines."""
        event = "message"
        data: list[str] = []
        while True:
            line = await asyncio.wait_for(
                self._lines.__anext__(), timeout=self._timeout
            )
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data.append(line.split(":", 1)[1].lstrip())
            elif line == "" and data:
                return event, "\n".join(data)
            # comments / retry directives / leading blanks fall through


def encode_frame(frame: dict[str, object]) -> bytes:
    """One LF-terminated wire line (kept local so helpers need no server import)."""
    return (json.dumps(frame, separators=(",", ":")) + "\n").encode()
