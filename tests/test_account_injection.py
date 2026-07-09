"""Wiring-integration test: per-thread active-account injection into Calendar calls.

Proves:
- Two threads bound to different accounts each produce the correct
  X-Account-Label header in the calendar MCP server config.
- The model never passes an account argument; the gate seam injects it.
- Concurrent threads bound to different accounts produce no cross-talk
  (each gets their own header value).
- GoogleService.server_config() includes headers when provided.
- TaskManager._session_kwargs produces the right calendar header when an
  active account is set.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.base import Attachment
from chief.core.session import Final
from chief.core.tasks import SessionProto, TaskManager
from chief.memory.versioning import NullVersioner
from chief.persistence.tasks import (
    get_or_create_task,
    set_active_account,
)
from chief.tools.google import GoogleService
from chief.tools.google.accounts import GoogleAccount
from chief.tools.google.set_account_service import SetAccountService

# ---------------------------------------------------------------------------
# GoogleService header injection
# ---------------------------------------------------------------------------


class TestGoogleServiceHeaders:
    """GoogleService.server_config() passes headers when provided."""

    def test_server_config_without_headers(self) -> None:
        svc = GoogleService(
            name="calendar",
            server_name="calendar",
            url="http://mcp-calendar:8003/mcp",
            read_tools=(),
            write_tools=(),
        )
        cfg = svc.server_config()
        assert cfg == {"type": "http", "url": "http://mcp-calendar:8003/mcp"}
        assert "headers" not in cfg

    def test_server_config_with_headers(self) -> None:
        svc = GoogleService(
            name="calendar",
            server_name="calendar",
            url="http://mcp-calendar:8003/mcp",
            read_tools=(),
            write_tools=(),
            headers={"X-Account-Label": "work@corp.com"},
        )
        cfg = svc.server_config()
        assert cfg == {
            "type": "http",
            "url": "http://mcp-calendar:8003/mcp",
            "headers": {"X-Account-Label": "work@corp.com"},
        }

    def test_server_config_with_empty_headers_omits_key(self) -> None:
        """Empty headers dict should be omitted (not passed as empty dict)."""
        svc = GoogleService(
            name="calendar",
            server_name="calendar",
            url="http://mcp-calendar:8003/mcp",
            read_tools=(),
            write_tools=(),
            headers={},
        )
        cfg = svc.server_config()
        assert "headers" not in cfg


# ---------------------------------------------------------------------------
# Calendar mcp.py factory with headers
# ---------------------------------------------------------------------------


class TestCalendarMcpServiceFactory:
    """calendar.mcp.service() can inject headers into the service config."""

    def test_service_with_account_header(self) -> None:
        from chief.tools.calendar import mcp

        svc = mcp.service(
            "http://mcp-calendar:8003/mcp",
            headers={"X-Account-Label": "work@corp.com"},
        )
        cfg = svc.server_config()
        assert cfg["headers"] == {"X-Account-Label": "work@corp.com"}

    def test_service_without_headers_unchanged(self) -> None:
        from chief.tools.calendar import mcp

        svc = mcp.service("http://mcp-calendar:8003/mcp")
        cfg = svc.server_config()
        assert "headers" not in cfg


# ---------------------------------------------------------------------------
# Per-thread active-account → injected header in session wiring
# ---------------------------------------------------------------------------


class TestPerThreadAccountInjection:
    """Two threads bound to different accounts each produce their account header."""

    def _make_accounts(self) -> list[GoogleAccount]:
        return [
            GoogleAccount(label="work@corp.com", email="work@corp.com"),
            GoogleAccount(label="personal@example.com", email="personal@example.com"),
        ]

    @pytest.mark.asyncio
    async def test_active_account_label_readable_after_set(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """SetAccountService.get_active_account_label returns the label after set."""
        svc = SetAccountService(
            accounts=self._make_accounts(),
            session_factory=session_factory,
            platform="telegram",
        )
        # Set account A on thread-A
        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:A",
                tier="owner",
            )
            await set_active_account(session, task, "work@corp.com")

        label = await svc.get_active_account_label("thread:A")
        assert label == "work@corp.com"

    @pytest.mark.asyncio
    async def test_two_threads_different_accounts_no_cross_talk(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Two threads bound to different accounts report own label independently."""
        svc = SetAccountService(
            accounts=self._make_accounts(),
            session_factory=session_factory,
            platform="telegram",
        )
        async with session_factory() as session:
            task_a = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:inject-A",
                tier="owner",
            )
            await set_active_account(session, task_a, "work@corp.com")

        async with session_factory() as session:
            task_b = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:inject-B",
                tier="owner",
            )
            await set_active_account(session, task_b, "personal@example.com")

        label_a = await svc.get_active_account_label("thread:inject-A")
        label_b = await svc.get_active_account_label("thread:inject-B")

        assert label_a == "work@corp.com"
        assert label_b == "personal@example.com"
        assert label_a != label_b  # no cross-talk

    @pytest.mark.asyncio
    async def test_concurrent_threads_no_cross_talk(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Concurrent async reads for two different threads return the right labels."""
        import asyncio

        svc = SetAccountService(
            accounts=self._make_accounts(),
            session_factory=session_factory,
            platform="telegram",
        )
        async with session_factory() as session:
            task_a = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:conc-A",
                tier="owner",
            )
            await set_active_account(session, task_a, "work@corp.com")

        async with session_factory() as session:
            task_b = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:conc-B",
                tier="owner",
            )
            await set_active_account(session, task_b, "personal@example.com")

        # Read both labels concurrently
        label_a, label_b = await asyncio.gather(
            svc.get_active_account_label("thread:conc-A"),
            svc.get_active_account_label("thread:conc-B"),
        )

        assert label_a == "work@corp.com"
        assert label_b == "personal@example.com"

    @pytest.mark.asyncio
    async def test_unset_thread_returns_none(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A thread with no active account set returns None."""
        svc = SetAccountService(
            accounts=self._make_accounts(),
            session_factory=session_factory,
            platform="telegram",
        )
        async with session_factory() as session:
            await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:unset",
                tier="owner",
            )

        label = await svc.get_active_account_label("thread:unset")
        assert label is None


# ---------------------------------------------------------------------------
# Model does not pass account argument: the injection happens in gate seam
# ---------------------------------------------------------------------------


class TestModelDoesNotPassAccount:
    """The model never passes an account argument; the gate injects it."""

    def test_calendar_read_tools_have_no_account_parameter(self) -> None:
        """None of the calendar tool names contain an 'account' parameter hint."""
        from chief.tools.calendar import mcp as calendar_mcp

        # The tool names themselves carry no 'account' — that's the model's view.
        # (The injection happens transparently in the HTTP header, not the tool call.)
        for tool in calendar_mcp.READ_TOOLS + calendar_mcp.WRITE_TOOLS:
            assert "account" not in tool.lower(), (
                f"Calendar tool name {tool!r} contains 'account' — "
                "the account argument must not be visible to the model."
            )


# ---------------------------------------------------------------------------
# TaskManager wiring integration: per-thread account injected into calendar config
# ---------------------------------------------------------------------------


class FakeMemory:
    """Minimal MemoryStore stub."""

    def __init__(self, memory_dir: str) -> None:
        self._dir = memory_dir
        self._versioner = NullVersioner()

    @property
    def versioner(self) -> NullVersioner:
        return self._versioner

    def facts_listing(self) -> str:
        return ""

    def soul(self) -> str:
        return "# Soul\nI am chief."

    def user(self) -> str:
        return "# User"

    def list_facts(self, namespace: str) -> list[Any]:
        return []

    async def forget(self, namespace: str, query: str) -> list[Any]:
        return []

    async def purge_expired(self) -> int:
        return 0

    async def ensure_scaffold(self) -> None:
        pass


class CaptureSession:
    """Captures the session kwargs passed at construction for inspection."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.session_id: str | None = None
        self.last_cost_usd: float = 0.0
        self.last_rate_limit_status: str | None = None
        self.last_served_model: str | None = None

    async def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> Any:
        yield Final(text="ok")

    async def interrupt(self) -> None:
        pass

    async def set_model(self, model: str) -> None:
        pass

    async def aclose(self) -> None:
        pass


def _make_session_capturer() -> tuple[
    list[CaptureSession],
    Any,
]:
    """Return a (sessions_list, factory) pair to capture session kwargs."""
    sessions: list[CaptureSession] = []

    def factory(**kwargs: Any) -> SessionProto:
        sess = CaptureSession(**kwargs)
        sessions.append(sess)
        return sess

    return sessions, factory


class TestTaskManagerAccountInjection:
    """Integration: per-thread account injected into calendar MCP server config."""

    def _make_manager(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        *,
        sessions: list[CaptureSession],
        sdk_factory: Any,
    ) -> TaskManager:
        from chief.tools.google import GoogleService
        from chief.tools.google.accounts import GoogleAccount
        from chief.tools.google.set_account_service import SetAccountService

        accounts = [
            GoogleAccount(label="work@corp.com", email="work@corp.com"),
            GoogleAccount(label="personal@example.com", email="personal@example.com"),
        ]
        set_account_svc = SetAccountService(
            accounts=accounts,
            session_factory=session_factory,
            platform="telegram",
        )
        calendar_svc = GoogleService(
            name="calendar",
            server_name="calendar",
            url="http://mcp-calendar:8003/mcp",
            read_tools=("mcp__calendar__list-calendars",),
            write_tools=(),
        )
        memory = FakeMemory(str(tmp_path / "memory"))
        return TaskManager(
            session_factory=session_factory,
            io=_FakeIO(),
            owner_model="claude-sonnet-4-6",
            classifier_model="claude-haiku-4-5",
            session_factory_sdk=sdk_factory,
            google_services=[calendar_svc],
            set_account_service=set_account_svc,
            memory=memory,
            memory_dir=str(tmp_path / "memory"),
        )

    @pytest.mark.asyncio
    async def test_active_account_injected_as_header(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Active account on thread → calendar MCP server config has label header."""
        sessions, factory = _make_session_capturer()
        mgr = self._make_manager(
            session_factory, tmp_path, sessions=sessions, sdk_factory=factory
        )

        # Set account A on thread-A in the DB
        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:wiring-A",
                tier="owner",
            )
            await set_active_account(session, task, "work@corp.com")

        # Trigger session creation for thread-A
        await mgr.dispatch(thread_key="thread:wiring-A", text="hello")

        assert sessions, "Expected session to be created"
        mcp_servers = sessions[0].kwargs.get("mcp_servers", {})
        cal_cfg = mcp_servers.get("calendar", {})
        headers = cal_cfg.get("headers", {})
        assert headers.get("X-Account-Label") == "work@corp.com", (
            f"Expected X-Account-Label=work@corp.com in calendar config, "
            f"got {headers!r}"
        )

        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_no_active_account_no_header_injected(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """When thread has no active account, calendar config has no account header."""
        sessions, factory = _make_session_capturer()
        mgr = self._make_manager(
            session_factory, tmp_path, sessions=sessions, sdk_factory=factory
        )

        # No set_active_account call — thread has no binding
        await mgr.dispatch(thread_key="thread:wiring-none", text="hello")

        assert sessions, "Expected session to be created"
        mcp_servers = sessions[0].kwargs.get("mcp_servers", {})
        cal_cfg = mcp_servers.get("calendar", {})
        headers = cal_cfg.get("headers", {})
        assert "X-Account-Label" not in headers, (
            f"Expected no X-Account-Label header when no account set, "
            f"got {headers!r}"
        )

        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_two_threads_different_headers_no_cross_talk(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Two threads on one manager get separate account headers — no cross-talk.

        Uses a single manager with two different thread_keys bound to different
        accounts. Dispatches sequentially so the in-memory SQLite StaticPool doesn't
        see concurrent access, but verifies that each session creation stamped the
        right label for its thread.
        """
        sessions: list[CaptureSession] = []
        _, factory = _make_session_capturer()

        def capturing_factory(**kwargs: Any) -> SessionProto:
            sess = CaptureSession(**kwargs)
            sessions.append(sess)
            return sess

        mgr = self._make_manager(
            session_factory, tmp_path, sessions=sessions, sdk_factory=capturing_factory
        )

        # Bind different accounts to two different threads
        async with session_factory() as session:
            task = await get_or_create_task(
                session, platform="telegram", thread_key="wiring-cross-A", tier="owner"
            )
            await set_active_account(session, task, "work@corp.com")

        async with session_factory() as session:
            task = await get_or_create_task(
                session, platform="telegram", thread_key="wiring-cross-B", tier="owner"
            )
            await set_active_account(session, task, "personal@example.com")

        # Dispatch sequentially — both threads on the same manager
        await mgr.dispatch(thread_key="wiring-cross-A", text="hi from A")
        await mgr.dispatch(thread_key="wiring-cross-B", text="hi from B")

        assert len(sessions) == 2, f"Expected 2 sessions, got {len(sessions)}"

        cal_a = sessions[0].kwargs.get("mcp_servers", {}).get("calendar", {})
        cal_b = sessions[1].kwargs.get("mcp_servers", {}).get("calendar", {})
        label_a = cal_a.get("headers", {}).get("X-Account-Label")
        label_b = cal_b.get("headers", {}).get("X-Account-Label")

        assert label_a == "work@corp.com", (
            f"Thread A expected work@corp.com, got {label_a!r}"
        )
        assert label_b == "personal@example.com", (
            f"Thread B expected personal@example.com, got {label_b!r}"
        )
        assert label_a != label_b, "Cross-talk: both threads got the same account label"

        await mgr.shutdown()


class _FakeIO:
    """Minimal TaskIO stub."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, str]] = []

    async def send(self, thread_key: str, text: str) -> None:
        self.sends.append((thread_key, text))

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None:
        pass

    async def create_thread(self, *, like_thread_key: str, title: str) -> str:
        return f"{like_thread_key}:child"

    async def archive_thread(self, thread_key: str) -> None:
        pass
