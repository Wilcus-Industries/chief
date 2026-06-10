"""Gate wiring and guest isolation for the browser (playwright) MCP service."""

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.base import Attachment
from chief.core.session import Final, TurnEvent
from chief.core.tasks import (
    MEMORY_TOOLS,
    WEB_META_TOOLS,
    SessionProto,
    TaskManager,
)
from chief.memory.store import Fact
from chief.memory.versioning import NullVersioner, Versioner
from chief.tools.browser import mcp as browser_mcp

Factory = Callable[..., SessionProto]


class _FakeMemory:
    def __init__(self) -> None:
        self._versioner: Versioner = NullVersioner()

    @property
    def versioner(self) -> Versioner:
        return self._versioner

    def facts_listing(self) -> str:
        return ""

    def soul(self) -> str:
        return "# Soul\nI am chief."

    def user(self) -> str:
        return "# Will"

    def list_facts(self, namespace: str) -> list[Fact]:
        return []

    async def forget(self, namespace: str, query: str) -> list[Fact]:
        raise NotImplementedError

    async def purge_expired(self) -> int:
        raise NotImplementedError

    async def ensure_scaffold(self) -> None:
        return None


class _FakeSession:
    def __init__(self, *, model: str, resume: str | None = None, **_: Any) -> None:
        self.model = model
        self.resume = resume
        self.session_id = resume
        self.last_cost_usd = 0.0
        self.last_rate_limit_status: str | None = None

    async def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> "AsyncIterator[TurnEvent]":
        yield Final(text=f"reply:{text}")

    async def interrupt(self) -> None:
        pass

    async def set_model(self, model: str) -> None:
        self.model = model

    async def aclose(self) -> None:
        pass


def _capture_factory(captured: dict[str, Any]) -> Factory:
    def factory(**kwargs: Any) -> SessionProto:
        captured.clear()
        captured.update(kwargs)
        return _FakeSession(model=kwargs["model"], resume=kwargs.get("resume"))

    return factory


async def _no(*args: Any, **kwargs: Any) -> bool:
    return False


def _browser_manager(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    factory: Factory,
    enabled: bool = True,
) -> TaskManager:
    services = (
        (browser_mcp.service("http://mcp-playwright:3000/mcp"),) if enabled else ()
    )
    return TaskManager(
        session_factory=session_factory,
        io=_FakeIO(),
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        memory=_FakeMemory(),
        memory_dir="/tmp/mem",
        owner_name="Will",
        google_services=services,
    )


class _FakeIO:
    async def send(self, thread_key: str, text: str) -> None:
        pass

    async def send_file(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def create_thread(self, *, like_thread_key: str, title: str) -> str:
        return "new"

    async def archive_thread(self, thread_key: str) -> None:
        pass


# ---- catalog unit tests (partition shape) ----------------------------------------


def test_service_shape_mirrors_google_services() -> None:
    svc = browser_mcp.service("http://mcp-playwright:3000/mcp")
    assert svc.name == "browser"
    assert svc.server_name == "playwright"
    assert svc.read_tools is browser_mcp.READ_TOOLS
    assert svc.write_tools is browser_mcp.WRITE_TOOLS
    assert svc.deferred_tools == ()
    assert svc.server_config() == {
        "type": "http",
        "url": "http://mcp-playwright:3000/mcp",
    }


# ---- owner session wiring --------------------------------------------------------


async def test_owner_browser_session_wires_mcp_and_read_tools_in_allowed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Read tools must be in allowed_tools so the gate pre-approves them (no card).
    captured: dict[str, Any] = {}
    mgr = _browser_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:5", tier="owner")

    assert captured["mcp_servers"] == {
        "playwright": {"type": "http", "url": "http://mcp-playwright:3000/mcp"}
    }
    allowed = captured["allowed_tools"]
    # A sample of read tools must be in allowed_tools.
    assert "mcp__playwright__browser_navigate" in allowed
    assert "mcp__playwright__browser_snapshot" in allowed
    assert "mcp__playwright__browser_take_screenshot" in allowed
    assert "mcp__playwright__browser_console_messages" in allowed
    assert "mcp__playwright__browser_wait_for" in allowed
    assert "mcp__playwright__browser_tabs" in allowed
    # Memory + web tools are retained alongside browser tools.
    assert "Read" in allowed
    assert "WebSearch" in allowed
    await mgr.shutdown()


async def test_owner_browser_write_tools_absent_from_allowed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Write tools must be absent from allowed_tools so they reach approval.
    captured: dict[str, Any] = {}
    mgr = _browser_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:5", tier="owner")

    allowed = set(captured["allowed_tools"])
    for write_tool in browser_mcp.WRITE_TOOLS:
        assert write_tool not in allowed, (
            f"{write_tool!r} must NOT be in allowed_tools (it is a write tool)"
        )
    await mgr.shutdown()


async def test_owner_browser_gate_read_set_includes_read_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Read tools are fed to the gate's extra_read_only so the PreToolUse hook
    # allows them with no card (same as calendar reads).
    # We verify this indirectly: the tools appear in allowed_tools AND they are all
    # in browser_mcp.READ_TOOLS (not in WRITE_TOOLS) — correct partition confirms
    # the gate sees them as pre-approved.
    captured: dict[str, Any] = {}
    mgr = _browser_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:5", tier="owner")

    allowed = set(captured["allowed_tools"])
    for read_tool in browser_mcp.READ_TOOLS:
        assert read_tool in allowed, (
            f"{read_tool!r} must be in allowed_tools (pre-approved read tool)"
        )
    await mgr.shutdown()


# ---- guest isolation tests -------------------------------------------------------


async def test_guest_session_has_no_browser_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Browser tools must be completely absent from guest sessions (tier isolation).
    captured: dict[str, Any] = {}
    mgr = _browser_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:9", tier="guest")

    allowed = set(captured.get("allowed_tools", []))
    # No browser tool — neither read nor write — must appear in a guest session.
    for tool in list(browser_mcp.READ_TOOLS) + list(browser_mcp.WRITE_TOOLS):
        assert tool not in allowed, (
            f"browser tool {tool!r} leaked into guest session"
        )
    # No playwright MCP server registered for the guest.
    assert "playwright" not in captured.get("mcp_servers", {})
    await mgr.shutdown()


async def test_guest_session_has_empty_allowed_tools_with_browser_manager(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A guest with no calendar/guest service wired gets an empty allowed list —
    # browser tools never bleed through even when the manager has them for the owner.
    captured: dict[str, Any] = {}
    mgr = _browser_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:9", tier="guest")

    # No mcp_servers for the guest (playwright is owner-only).
    assert "mcp_servers" not in captured
    # allowed_tools is empty — no browser or owner tools leaked.
    assert captured["allowed_tools"] == []
    await mgr.shutdown()


# ---- disabled browser -------------------------------------------------------


async def test_browser_disabled_owner_keeps_memory_and_web_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _browser_manager(
        session_factory, factory=_capture_factory(captured), enabled=False
    )

    await mgr._ensure_task("-100:5", tier="owner")

    # No playwright MCP server when disabled.
    assert "mcp_servers" not in captured
    # Owner still gets memory + web tools.
    assert set(captured["allowed_tools"]) == set(MEMORY_TOOLS) | set(WEB_META_TOOLS)
    await mgr.shutdown()
