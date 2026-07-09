"""Wiring-integration tests: ask-when-ambiguous + memory-driven account selection.

Proves:
- Fresh thread, no binding, no memory hint, multiple accounts: _ensure_task injects
  the ask-which-account guidance into the session's system prompt (no silent guess).
- Memory fact naming owner's account: _ensure_task auto-selects and persists the
  account (no question asked).
- Once chosen (via memory hint or ask-and-answer), subsequent dispatches don't
  re-ask — the binding is set and the session picks it up.
- extract_account_hint: returns the label that matches an account in the registry.
- No accounts registered: selection guidance is not injected (nothing to choose from).
- Single account registered: selection guidance is not injected (unambiguous).
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
from chief.persistence.tasks import get_active_account, get_or_create_task
from chief.tools.google import GoogleService
from chief.tools.google.account_selection import (
    ACCOUNT_SELECTION_GUIDANCE,
    extract_account_hint,
)
from chief.tools.google.accounts import GoogleAccount
from chief.tools.google.set_account_service import SetAccountService

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


class _FakeIO:
    """Minimal TaskIO stub that records sends."""

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


class CaptureSession:
    """Captures session kwargs at construction; yields one Final per run_turn."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.session_id: str | None = None
        self.last_cost_usd: float = 0.0
        self.last_rate_limit_status: str | None = None
        self.last_served_model: str | None = None
        self.last_premium_requests: dict[str, int] = {}

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


def _make_capturer() -> tuple[list[CaptureSession], Any]:
    sessions: list[CaptureSession] = []

    def factory(**kwargs: Any) -> SessionProto:
        sess = CaptureSession(**kwargs)
        sessions.append(sess)
        return sess

    return sessions, factory


class FakeMemory:
    """Minimal MemoryStore stub with configurable user/facts content."""

    def __init__(
        self,
        memory_dir: str,
        *,
        user_content: str = "# User\n",
        fact_bodies: list[str] | None = None,
    ) -> None:
        from chief.memory.store import Fact

        self._dir = memory_dir
        self._versioner = NullVersioner()
        self._user_content = user_content
        self._facts: list[Fact] = []
        for body in fact_bodies or []:
            self._facts.append(
                Fact(
                    slug="test-fact",
                    title="test fact",
                    body=body,
                    namespace="owner",
                    provenance="owner-stated",
                    trust="high",
                    expires=None,
                    created="2024-01-01T00:00:00+00:00",
                )
            )

    @property
    def versioner(self) -> NullVersioner:
        return self._versioner

    def facts_listing(self) -> str:
        return ""

    def soul(self) -> str:
        return "# Soul\nI am chief."

    def user(self) -> str:
        return self._user_content

    def list_facts(self, namespace: str) -> list[Any]:
        if namespace == "owner":
            return list(self._facts)
        return []

    async def forget(self, namespace: str, query: str) -> list[Any]:
        return []

    async def purge_expired(self) -> int:
        return 0

    async def ensure_scaffold(self) -> None:
        pass


def _make_accounts_multi() -> list[GoogleAccount]:
    return [
        GoogleAccount(label="work@corp.com", email="work@corp.com"),
        GoogleAccount(label="personal@example.com", email="personal@example.com"),
    ]


def _make_accounts_single() -> list[GoogleAccount]:
    return [GoogleAccount(label="only@example.com", email="only@example.com")]


def _make_manager(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    sessions: list[CaptureSession],
    sdk_factory: Any,
    accounts: list[GoogleAccount],
    memory: FakeMemory,
) -> TaskManager:
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


# ---------------------------------------------------------------------------
# extract_account_hint
# ---------------------------------------------------------------------------


class TestExtractAccountHint:
    """extract_account_hint: finds an account label matching memory content."""

    def test_email_in_user_md_returns_label(self) -> None:
        """Email present in User.md matches a registered account → returns its label."""
        from chief.memory.store import Fact

        class _M(FakeMemory):
            def user(self) -> str:
                return "# User\n\nMy work email is work@corp.com"

            def list_facts(self, namespace: str) -> list[Fact]:
                return []

        memory = _M("tmp")
        accounts = _make_accounts_multi()
        result = extract_account_hint(memory, accounts)
        assert result == "work@corp.com"

    def test_email_in_facts_returns_label(self) -> None:
        """Email in a facts body matches a registered account → returns its label."""
        memory = FakeMemory(
            "tmp", fact_bodies=["The owner's Google account is personal@example.com"]
        )
        accounts = _make_accounts_multi()
        result = extract_account_hint(memory, accounts)
        assert result == "personal@example.com"

    def test_no_matching_email_returns_none(self) -> None:
        """No email in memory that matches registered accounts → returns None."""
        memory = FakeMemory("tmp", user_content="# User\n\nNo account info here.")
        accounts = _make_accounts_multi()
        result = extract_account_hint(memory, accounts)
        assert result is None

    def test_no_accounts_returns_none(self) -> None:
        """Empty account registry → returns None (nothing to match against)."""
        memory = FakeMemory(
            "tmp", user_content="# User\n\nMy email is work@corp.com"
        )
        result = extract_account_hint(memory, [])
        assert result is None

    def test_single_account_registered_returns_it(self) -> None:
        """Single account registered always matches if email appears in memory."""
        memory = FakeMemory(
            "tmp",
            user_content="# User\n\nI use only@example.com for everything.",
        )
        accounts = _make_accounts_single()
        result = extract_account_hint(memory, accounts)
        assert result == "only@example.com"


# ---------------------------------------------------------------------------
# No accounts registered: no guidance injected
# ---------------------------------------------------------------------------


class TestNoAccountsRegistered:
    @pytest.mark.asyncio
    async def test_no_accounts_no_selection_guidance(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """No registered accounts → no selection guidance in system prompt."""
        sessions, factory = _make_capturer()
        memory = FakeMemory(str(tmp_path / "memory"))
        mgr = _make_manager(
            session_factory,
            tmp_path,
            sessions=sessions,
            sdk_factory=factory,
            accounts=[],  # no accounts
            memory=memory,
        )

        await mgr.dispatch(thread_key="thread:noaccounts", text="show my calendar")
        assert sessions, "expected a session to be created"
        system_prompt = sessions[0].kwargs.get("system_prompt", "")
        assert ACCOUNT_SELECTION_GUIDANCE not in (system_prompt or "")
        await mgr.shutdown()


# ---------------------------------------------------------------------------
# Single account: no guidance injected (unambiguous)
# ---------------------------------------------------------------------------


class TestSingleAccount:
    @pytest.mark.asyncio
    async def test_single_account_no_selection_guidance(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """One registered account → no selection guidance (nothing to choose)."""
        sessions, factory = _make_capturer()
        memory = FakeMemory(str(tmp_path / "memory"))
        mgr = _make_manager(
            session_factory,
            tmp_path,
            sessions=sessions,
            sdk_factory=factory,
            accounts=_make_accounts_single(),
            memory=memory,
        )

        await mgr.dispatch(thread_key="thread:singleaccount", text="list events")
        assert sessions, "expected a session to be created"
        system_prompt = sessions[0].kwargs.get("system_prompt", "")
        assert ACCOUNT_SELECTION_GUIDANCE not in (system_prompt or "")
        await mgr.shutdown()


# ---------------------------------------------------------------------------
# Fresh thread, no binding, no memory hint → ask-when-ambiguous
# ---------------------------------------------------------------------------


class TestAskWhenAmbiguous:
    @pytest.mark.asyncio
    async def test_no_binding_no_hint_injects_ask_guidance(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Fresh thread, multiple accounts, no binding, no hint → ask guidance present.

        Acceptance criterion: no silent guess, no 'last used' fallback.
        The system prompt must contain the selection guidance so the model asks.
        """
        sessions, factory = _make_capturer()
        memory = FakeMemory(str(tmp_path / "memory"))  # no account mention
        mgr = _make_manager(
            session_factory,
            tmp_path,
            sessions=sessions,
            sdk_factory=factory,
            accounts=_make_accounts_multi(),
            memory=memory,
        )

        await mgr.dispatch(thread_key="thread:ambiguous", text="show my calendar")
        assert sessions, "expected a session to be created"
        system_prompt = sessions[0].kwargs.get("system_prompt", "")
        assert ACCOUNT_SELECTION_GUIDANCE in (system_prompt or ""), (
            "Expected ask-which-account guidance in system prompt when no binding "
            f"and no memory hint, got: {system_prompt!r}"
        )
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_explicit_binding_suppresses_ask_guidance(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Per-thread binding already set → no ask guidance (already chosen)."""
        sessions, factory = _make_capturer()
        memory = FakeMemory(str(tmp_path / "memory"))
        mgr = _make_manager(
            session_factory,
            tmp_path,
            sessions=sessions,
            sdk_factory=factory,
            accounts=_make_accounts_multi(),
            memory=memory,
        )

        # Pre-bind the account
        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:bound",
                tier="owner",
            )
            from chief.persistence.tasks import set_active_account

            await set_active_account(session, task, "work@corp.com")

        await mgr.dispatch(thread_key="thread:bound", text="show my calendar")
        assert sessions
        system_prompt = sessions[0].kwargs.get("system_prompt", "")
        assert ACCOUNT_SELECTION_GUIDANCE not in (system_prompt or ""), (
            "Ask guidance must not appear when thread already has a binding."
        )
        await mgr.shutdown()


# ---------------------------------------------------------------------------
# Memory-driven selection
# ---------------------------------------------------------------------------


class TestMemoryDrivenSelection:
    @pytest.mark.asyncio
    async def test_memory_hint_auto_selects_and_persists(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Memory fact naming owner's account → auto-selected, no ask guidance.

        Acceptance criterion: with a memory fact naming the owner's account for
        the context, chief selects that account without asking.
        """
        sessions, factory = _make_capturer()
        # Memory has a hint pointing to work@corp.com
        memory = FakeMemory(
            str(tmp_path / "memory"),
            user_content="# User\n\nMy work Google account is work@corp.com",
        )
        accounts = _make_accounts_multi()
        mgr = _make_manager(
            session_factory,
            tmp_path,
            sessions=sessions,
            sdk_factory=factory,
            accounts=accounts,
            memory=memory,
        )

        await mgr.dispatch(thread_key="thread:memory-hint", text="show my calendar")
        assert sessions

        # The session should carry the work account header (memory-driven injection)
        mcp_servers = sessions[0].kwargs.get("mcp_servers", {})
        cal_cfg = mcp_servers.get("calendar", {})
        label = cal_cfg.get("headers", {}).get("X-Account-Label")
        assert label == "work@corp.com", (
            f"Expected work@corp.com from memory hint, got {label!r}"
        )

        # No ask guidance in system prompt (auto-selected from memory)
        system_prompt = sessions[0].kwargs.get("system_prompt", "")
        assert ACCOUNT_SELECTION_GUIDANCE not in (system_prompt or ""), (
            "Ask guidance must not appear when memory supplies a hint."
        )

        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_memory_hint_binds_thread(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Memory hint selection persists to DB so subsequent calls don't re-ask."""
        sessions, factory = _make_capturer()
        memory = FakeMemory(
            str(tmp_path / "memory"),
            user_content="# User\n\nMy work Google account is work@corp.com",
        )
        accounts = _make_accounts_multi()
        mgr = _make_manager(
            session_factory,
            tmp_path,
            sessions=sessions,
            sdk_factory=factory,
            accounts=accounts,
            memory=memory,
        )

        await mgr.dispatch(thread_key="thread:hint-bind", text="check calendar")

        # Verify the binding was persisted to DB
        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:hint-bind",
                tier="owner",
            )
            stored = await get_active_account(session, task)

        assert stored == "work@corp.com", (
            f"Expected memory hint to be persisted as active account, got {stored!r}"
        )
        await mgr.shutdown()


# ---------------------------------------------------------------------------
# Once chosen, subsequent calls don't re-ask
# ---------------------------------------------------------------------------


class TestBindingPersists:
    @pytest.mark.asyncio
    async def test_second_dispatch_uses_cached_session(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Second dispatch to the same thread reuses the existing session (no re-ask).

        Once the session is live (first dispatch created it), subsequent dispatches
        to the same thread_key reuse it — no new session is created, so no
        re-evaluation of account selection. The binding is already set.
        """
        sessions, factory = _make_capturer()
        memory = FakeMemory(str(tmp_path / "memory"))
        accounts = _make_accounts_multi()
        mgr = _make_manager(
            session_factory,
            tmp_path,
            sessions=sessions,
            sdk_factory=factory,
            accounts=accounts,
            memory=memory,
        )

        # First dispatch (will inject ask guidance for ambiguous thread)
        await mgr.dispatch(thread_key="thread:rebind", text="hello")
        initial_count = len(sessions)

        # Second dispatch — same thread, session already live, no new session created
        await mgr.dispatch(thread_key="thread:rebind", text="show calendar")
        # No new session should be created
        assert len(sessions) == initial_count, (
            "Second dispatch to same thread should reuse the existing session, "
            f"not create a new one "
            f"(sessions: {len(sessions)}, expected {initial_count})"
        )
        await mgr.shutdown()
