"""classify() — the single source of truth for the gate's per-tier decision order.

Owner: NEVER → DENY · built-in shell → DENY · APPROVED → ALLOW · blacklist → ASK ·
else ALLOW (default-allow). Guest: the original default-ask posture, unchanged.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.gate.blacklist import Blacklist
from chief.gate.gate import GateDecision, classify, is_read_only
from chief.gate.policy import PolicyStore

BLACKLIST = Blacklist.from_config()


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


# ---- both tiers: NEVER + built-in shell ---------------------------------------


async def test_never_denies_on_both_tiers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(
        session_factory, never=[("mcp__chief_shell__bash", "rm -rf /tmp/x")]
    )

    for tier in ("owner", "guest"):
        verdict = classify(
            "mcp__chief_shell__bash",
            {"command": "rm -rf /tmp/x"},
            store,
            tier=tier,
            blacklist=BLACKLIST,
        )
        assert verdict.decision is GateDecision.DENY, tier


async def test_never_wins_over_read_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A NEVER rule on a genuinely read-only tool (Read) still hard-denies, both tiers.
    store = await _store(session_factory, never=[("Read", None)])

    for tier in ("owner", "guest"):
        verdict = classify("Read", {"file_path": "/tmp/x"}, store, tier=tier)
        assert verdict.decision is GateDecision.DENY, tier


async def test_builtin_shell_tools_denied_on_both_tiers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # One shell surface only: the built-ins are refused so every command runs through
    # the per-task host shell tool (and its blacklist matching).
    store = await _store(session_factory)

    for tier in ("owner", "guest"):
        for name in ("Bash", "BashOutput", "KillShell"):
            verdict = classify(name, {"command": "env"}, store, tier=tier)
            assert verdict.decision is GateDecision.DENY, (tier, name)
            assert "bash tool" in verdict.reason


async def test_builtin_shell_denied_over_approved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An APPROVED rule must NOT resurrect the built-in shell — one surface only.
    store = await _store(session_factory, approved=[("Bash", "env")])

    verdict = classify("Bash", {"command": "env"}, store, tier="owner")

    assert verdict.decision is GateDecision.DENY


# ---- owner tier: default-allow + blacklist -------------------------------------


async def test_owner_effectful_tool_allows_by_default(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify(
        "mcp__gcal__create-event",
        {"summary": "standup"},
        store,
        tier="owner",
        blacklist=BLACKLIST,
    )

    assert verdict.decision is GateDecision.ALLOW
    assert "default" in verdict.reason


async def test_owner_shell_command_allows_by_default(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify(
        "mcp__chief_shell__bash",
        {"command": "git status"},
        store,
        tier="owner",
        blacklist=BLACKLIST,
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_owner_blacklisted_command_asks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify(
        "mcp__chief_shell__bash",
        {"command": "sudo rm -rf /var/lib"},
        store,
        tier="owner",
        blacklist=BLACKLIST,
    )

    assert verdict.decision is GateDecision.ASK
    assert "blacklist" in verdict.reason


async def test_owner_blacklisted_tool_asks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)
    blacklist = Blacklist.from_config(tools=("mcp__gmail_chief__send-email",))

    verdict = classify(
        "mcp__gmail_chief__send-email",
        {"to": "x@y.z"},
        store,
        tier="owner",
        blacklist=blacklist,
    )

    assert verdict.decision is GateDecision.ASK


async def test_owner_approved_rule_beats_blacklist(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An explicit "always allow" blessing wins over a blacklist match — the
    # self-curating APPROVED list keeps meaning something under default-allow.
    store = await _store(
        session_factory,
        approved=[("mcp__chief_shell__bash", "sudo systemctl restart chief")],
    )

    verdict = classify(
        "mcp__chief_shell__bash",
        {"command": "sudo systemctl restart chief"},
        store,
        tier="owner",
        blacklist=BLACKLIST,
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_owner_writes_anywhere_allow(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Path confinement is gone: owner Write/Edit/Read anywhere is ALLOW by default.
    store = await _store(session_factory)

    for tool, args in (
        ("Write", {"file_path": "/etc/motd", "content": "x"}),
        ("Edit", {"file_path": "/home/owner/notes.md"}),
        ("Read", {"file_path": "/var/log/syslog"}),
        ("Glob", {"pattern": "**/*", "path": "/"}),
    ):
        verdict = classify(tool, args, store, tier="owner", blacklist=BLACKLIST)
        assert verdict.decision is GateDecision.ALLOW, tool


async def test_owner_no_blacklist_still_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # blacklist=None (unwired) means nothing asks — allowed by default.
    store = await _store(session_factory)

    verdict = classify(
        "mcp__chief_shell__bash", {"command": "sudo ls"}, store, tier="owner"
    )

    assert verdict.decision is GateDecision.ALLOW


# ---- guest tier: default-ask, unchanged ----------------------------------------


async def test_guest_effectful_tool_asks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify("mcp__gcal__create-event", {}, store, tier="guest")

    assert verdict.decision is GateDecision.ASK
    assert "approval" in verdict.reason


async def test_default_tier_is_guest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A caller that forgets tier fails closed (ASK), never open.
    store = await _store(session_factory)

    verdict = classify("mcp__notion__create-page", {"title": "x"}, store)

    assert verdict.decision is GateDecision.ASK


async def test_guest_file_ops_deny(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Guests have no file tools; a call must DENY, never fall through to read-only.
    store = await _store(session_factory)

    for tool, args in (
        ("Read", {"file_path": "/etc/passwd"}),
        ("Glob", {"pattern": "**/*"}),
        ("Grep", {"pattern": "x"}),
        ("Write", {"file_path": "/tmp/x", "content": "x"}),
        ("Edit", {"file_path": "/tmp/x"}),
    ):
        verdict = classify(tool, args, store, tier="guest")
        assert verdict.decision is GateDecision.DENY, tool


async def test_guest_unknown_tool_asks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # #88: WebSearch is no longer a known read-only built-in, so a guest call to it is
    # an unknown tool ⇒ the safe default ASK (not ALLOW).
    store = await _store(session_factory)

    verdict = classify("WebSearch", {"query": "x"}, store, tier="guest")

    assert verdict.decision is GateDecision.ASK


async def test_guest_extra_read_only_tool_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An MCP read tool the caller declared read-only (e.g. calendar free/busy) ALLOWs.
    store = await _store(session_factory)

    verdict = classify(
        "mcp__gcal__get-freebusy",
        {},
        store,
        tier="guest",
        extra_read_only=frozenset({"mcp__gcal__get-freebusy"}),
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_guest_calendar_write_not_in_read_set_asks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The guest booking write still reaches the Front Desk card.
    store = await _store(session_factory)

    verdict = classify(
        "mcp__gcal__create-event",
        {},
        store,
        tier="guest",
        extra_read_only=frozenset({"mcp__gcal__get-freebusy"}),
    )

    assert verdict.decision is GateDecision.ASK


async def test_never_wins_over_extra_read_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory, never=[("mcp__gcal__get-freebusy", None)])

    verdict = classify(
        "mcp__gcal__get-freebusy",
        {},
        store,
        tier="guest",
        extra_read_only=frozenset({"mcp__gcal__get-freebusy"}),
    )

    assert verdict.decision is GateDecision.DENY


async def test_guest_approved_rule_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(
        session_factory, approved=[("mcp__gcal__create-event", None)]
    )

    verdict = classify("mcp__gcal__create-event", {}, store, tier="guest")

    assert verdict.decision is GateDecision.ALLOW


async def test_guest_never_gains_owner_blacklist_posture(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Even if a blacklist is (wrongly) passed for a guest, an unlisted effectful tool
    # still ASKs — the default-allow branch is owner-only.
    store = await _store(session_factory)

    verdict = classify(
        "mcp__notion__create-page",
        {"title": "x"},
        store,
        tier="guest",
        blacklist=BLACKLIST,
    )

    assert verdict.decision is GateDecision.ASK
