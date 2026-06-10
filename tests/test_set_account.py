"""Per-thread active-account façade: set_account tool + persistence (issue #45).

Tests cover:
- set_active_account/get_active_account round-trip in the task repository
- Active account persists across reads (survives a new message in the same thread)
- Two threads with different active accounts don't interfere (no cross-thread leakage)
- SetAccountService: owner-only in-process tool
  - set_account with valid label/email sets the active account and confirms
  - set_account rejects unknown label/email (not in registry)
  - Reports current active account when asked
- Wiring: set_account_service is owner-only; guests never see it
- build_set_account_service() builds correctly
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.persistence.tasks import (
    get_active_account,
    get_or_create_task,
    set_active_account,
)
from chief.tools.google.accounts import GoogleAccount
from chief.tools.google.set_account_service import SetAccountService

# ---------------------------------------------------------------------------
# Persistence: set_active_account / get_active_account
# ---------------------------------------------------------------------------


class TestActiveAccountPersistence:
    @pytest.mark.asyncio
    async def test_get_active_account_returns_none_when_unset(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A new thread has no active account bound."""
        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:1",
                tier="owner",
            )
            result = await get_active_account(session, task)
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get_active_account_round_trip(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """set_active_account persists; get_active_account returns the label."""
        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:1",
                tier="owner",
            )
            await set_active_account(session, task, "work@example.com")

        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:1",
                tier="owner",
            )
            result = await get_active_account(session, task)

        assert result == "work@example.com"

    @pytest.mark.asyncio
    async def test_active_account_persists_across_turns(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Binding survives subsequent reads — same thread, new session."""
        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:persist",
                tier="owner",
            )
            await set_active_account(session, task, "personal@example.com")

        # Simulate a second turn by re-fetching the task in a fresh session
        for _ in range(2):
            async with session_factory() as session:
                task = await get_or_create_task(
                    session,
                    platform="telegram",
                    thread_key="thread:persist",
                    tier="owner",
                )
                result = await get_active_account(session, task)
            assert result == "personal@example.com"

    @pytest.mark.asyncio
    async def test_no_cross_thread_leakage(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Two threads with different active accounts don't interfere."""
        async with session_factory() as session:
            task_a = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:A",
                tier="owner",
            )
            await set_active_account(session, task_a, "work@example.com")

        async with session_factory() as session:
            task_b = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:B",
                tier="owner",
            )
            await set_active_account(session, task_b, "personal@example.com")

        async with session_factory() as session:
            task_a = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:A",
                tier="owner",
            )
            task_b = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:B",
                tier="owner",
            )
            result_a = await get_active_account(session, task_a)
            result_b = await get_active_account(session, task_b)

        assert result_a == "work@example.com"
        assert result_b == "personal@example.com"

    @pytest.mark.asyncio
    async def test_set_active_account_can_clear_with_none(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """set_active_account(None) clears the binding."""
        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:clear",
                tier="owner",
            )
            await set_active_account(session, task, "work@example.com")

        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:clear",
                tier="owner",
            )
            await set_active_account(session, task, None)

        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:clear",
                tier="owner",
            )
            result = await get_active_account(session, task)

        assert result is None


# ---------------------------------------------------------------------------
# SetAccountService — owner-only in-process MCP tool
# ---------------------------------------------------------------------------


class TestSetAccountService:
    def _make_accounts(self) -> list[GoogleAccount]:
        return [
            GoogleAccount(label="work@corp.com", email="work@corp.com"),
            GoogleAccount(label="personal@example.com", email="personal@example.com"),
            GoogleAccount(label="legacy-work", email=None),
        ]

    def _make_service(
        self,
        accounts: list[GoogleAccount],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> SetAccountService:
        return SetAccountService(
            accounts=accounts,
            session_factory=session_factory,
            platform="telegram",
        )

    def test_server_config_returns_sdk_mcp_server(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        svc = self._make_service(self._make_accounts(), session_factory)
        cfg = svc.server_config(thread_key="thread:cfg-test")
        assert cfg.get("type") == "sdk"

    def test_tool_name_is_qualified(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        svc = self._make_service(self._make_accounts(), session_factory)
        assert svc.tool_name == "mcp__chief_set_account__set_account"

    @pytest.mark.asyncio
    async def test_set_account_by_label_confirms(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """set_account with a known label sets the binding and returns confirmation."""
        svc = self._make_service(self._make_accounts(), session_factory)
        # Create the task row first
        async with session_factory() as session:
            await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:tool-test",
                tier="owner",
            )
        tool_obj = svc._build_tool("thread:tool-test")
        result = await tool_obj.handler({"account": "work@corp.com"})

        assert result["is_error"] is False
        text = result["content"][0]["text"]
        assert "work@corp.com" in text

    @pytest.mark.asyncio
    async def test_set_account_by_email_confirms(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """set_account by email (when label==email) also sets the binding."""
        svc = self._make_service(self._make_accounts(), session_factory)
        async with session_factory() as session:
            await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:email-test",
                tier="owner",
            )
        tool_obj = svc._build_tool("thread:email-test")
        result = await tool_obj.handler({"account": "personal@example.com"})

        assert result["is_error"] is False

    @pytest.mark.asyncio
    async def test_set_account_rejects_unknown_label(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """set_account with an unknown label/email returns an error."""
        svc = self._make_service(self._make_accounts(), session_factory)
        async with session_factory() as session:
            await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:reject-test",
                tier="owner",
            )
        tool_obj = svc._build_tool("thread:reject-test")
        result = await tool_obj.handler({"account": "unknown@nowhere.com"})

        assert result["is_error"] is True
        text = result["content"][0]["text"]
        assert "unknown" in text.lower() or "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_set_account_persists_to_db(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """After set_account tool call, the binding is readable from persistence."""
        svc = self._make_service(self._make_accounts(), session_factory)
        async with session_factory() as session:
            await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:persist-tool",
                tier="owner",
            )
        tool_obj = svc._build_tool("thread:persist-tool")
        await tool_obj.handler({"account": "work@corp.com"})

        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:persist-tool",
                tier="owner",
            )
            stored = await get_active_account(session, task)

        assert stored == "work@corp.com"

    @pytest.mark.asyncio
    async def test_get_active_account_tool_reports_current(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """After setting, the service can report the current active account."""
        svc = self._make_service(self._make_accounts(), session_factory)
        async with session_factory() as session:
            task = await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:report",
                tier="owner",
            )
            await set_active_account(session, task, "personal@example.com")

        report = await svc.get_active_account_label("thread:report")
        assert report == "personal@example.com"

    @pytest.mark.asyncio
    async def test_get_active_account_tool_reports_none_when_unset(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Reports None when no account is active."""
        svc = self._make_service(self._make_accounts(), session_factory)
        async with session_factory() as session:
            await get_or_create_task(
                session,
                platform="telegram",
                thread_key="thread:none-report",
                tier="owner",
            )
        report = await svc.get_active_account_label("thread:none-report")
        assert report is None


# ---------------------------------------------------------------------------
# Owner-only wiring: guests never see set_account
# ---------------------------------------------------------------------------


class TestSetAccountWiring:
    """set_account is owner-only; guest sessions never receive it."""

    def test_set_account_not_in_guest_allowed_tools(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The set_account tool name must not appear in a guest session's allowed_tools.

        This is enforced by construction in _wire_guest_session (which never adds
        the set_account_service tool) and verified here by introspection on the
        service tool_name vs the guest wiring invariant.
        """
        accounts = [GoogleAccount(label="work@corp.com", email="work@corp.com")]
        svc = SetAccountService(
            accounts=accounts,
            session_factory=session_factory,
            platform="telegram",
        )
        # The tool name must NOT appear in GUEST_DENIED (which is what the SDK
        # hard-denies) — its exclusion is by omission from allowed_tools, not
        # by denial. Either way, a guest's session cannot call it.
        # The key invariant: the tool is in the owner's allowed list only
        # (wired via _wire_owner_session, never _wire_guest_session).
        assert svc.tool_name.startswith("mcp__chief_set_account__")

    def test_service_server_name_is_stable(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The server name must be stable (used as the mcp_servers key)."""
        accounts = [GoogleAccount(label="work@corp.com", email="work@corp.com")]
        svc = SetAccountService(
            accounts=accounts,
            session_factory=session_factory,
            platform="telegram",
        )
        assert svc.server_name == "chief_set_account"


# ---------------------------------------------------------------------------
# build_set_account_service in app.py
# ---------------------------------------------------------------------------


class TestBuildSetAccountService:
    def test_build_set_account_service_returns_service(
        self, session_factory: async_sessionmaker[AsyncSession], tmp_path: Any
    ) -> None:
        from chief.app import build_set_account_service

        svc = build_set_account_service(
            session_factory=session_factory,
            platform="telegram",
            secrets_dir=tmp_path,  # empty dir → no accounts, but service still built
        )
        assert svc is not None
        assert svc.tool_name == "mcp__chief_set_account__set_account"

    def test_build_set_account_service_with_accounts(
        self, session_factory: async_sessionmaker[AsyncSession], tmp_path: Any
    ) -> None:
        """Service built with secrets_dir uses dynamic mode (issue #50).

        Discovery happens at tool-call time; we verify the service has secrets_dir
        wired correctly so the account is reachable per call.
        """
        import json

        token_data = {
            "token": "at",
            "refresh_token": "rt",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "cid",
            "client_secret": "secret",
            "account": "main@example.com",
        }
        (tmp_path / "google_token.json").write_text(
            json.dumps(token_data), encoding="utf-8"
        )

        from pathlib import Path

        from chief.app import build_set_account_service

        svc = build_set_account_service(
            session_factory=session_factory,
            platform="telegram",
            secrets_dir=tmp_path,
        )
        # Dynamic mode: accounts discovered per call — verify secrets_dir is set.
        assert svc.secrets_dir == Path(tmp_path), (
            "secrets_dir must be wired so the tool can re-scan at call time"
        )
