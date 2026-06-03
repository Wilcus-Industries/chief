"""classify() — the single source of truth for the gate's decision order."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.gate.gate import GateDecision, classify, is_read_only
from chief.gate.policy import PolicyStore


async def _store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    never: list[tuple[str, str | None]] | None = None,
    approved: list[tuple[str, str | None]] | None = None,
) -> PolicyStore:
    store = PolicyStore(session_factory)
    await store.seed(never=never or [], approved=approved or [])
    return store


def test_is_read_only_known_and_unknown() -> None:
    assert is_read_only("Read", {"file_path": "/x"}) is True
    assert is_read_only("Bash", {"command": "ls"}) is False


async def test_never_denies_with_no_prompt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory, never=[("Bash", "rm -rf /tmp/x")])

    verdict = classify("Bash", {"command": "rm -rf /tmp/x"}, store)

    assert verdict.decision is GateDecision.DENY


async def test_never_wins_over_read_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A NEVER rule on a normally read-only tool still hard-denies.
    store = await _store(session_factory, never=[("Read", None)])

    verdict = classify("Read", {"file_path": "/etc/shadow"}, store)

    assert verdict.decision is GateDecision.DENY


async def test_read_only_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify("Grep", {"pattern": "x"}, store)

    assert verdict.decision is GateDecision.ALLOW


async def test_approved_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory, approved=[("Bash", "git status")])

    verdict = classify("Bash", {"command": "git status"}, store)

    assert verdict.decision is GateDecision.ALLOW


async def test_unknown_effectful_tool_asks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify("Bash", {"command": "git push"}, store)

    assert verdict.decision is GateDecision.ASK
    assert "approval" in verdict.reason
