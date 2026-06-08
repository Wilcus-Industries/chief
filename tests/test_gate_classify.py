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
    store = await _store(
        session_factory, never=[("mcp__chief_shell__bash", "rm -rf /tmp/x")]
    )

    verdict = classify(
        "mcp__chief_shell__bash", {"command": "rm -rf /tmp/x"}, store
    )

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
    # A non-file read-only tool ALLOWs with no card and needs no configured root.
    store = await _store(session_factory)

    verdict = classify("WebSearch", {"query": "x"}, store)

    assert verdict.decision is GateDecision.ALLOW


async def test_file_op_with_no_root_denies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A file read with no configured root (e.g. a guest, who gets no file tools) must
    # DENY — never fall through to the read-only ALLOW, which would read anywhere.
    store = await _store(session_factory)

    for tool, args in (
        ("Read", {"file_path": "/etc/passwd"}),
        ("Glob", {"pattern": "**/*"}),
        ("Grep", {"pattern": "x"}),
    ):
        verdict = classify(tool, args, store)
        assert verdict.decision is GateDecision.DENY


async def test_builtin_shell_denied_over_approved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The built-in Bash runs inside core (Max token in env). An APPROVED rule must NOT
    # resurrect it — the hard DENY overrides the allow-list. (APPROVED → ALLOW for a
    # normal command tool is covered by test_shell_tool_approved_allows.)
    store = await _store(session_factory, approved=[("Bash", "env")])

    verdict = classify("Bash", {"command": "env"}, store)

    assert verdict.decision is GateDecision.DENY


async def test_unknown_effectful_tool_asks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify("mcp__notion__create-page", {"title": "x"}, store)

    assert verdict.decision is GateDecision.ASK
    assert "approval" in verdict.reason


async def test_builtin_shell_tools_denied(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Bash/BashOutput/KillShell all execute in core — each is a hard DENY so the model
    # is forced onto the secret-free sandbox shell, even with file scoping wired.
    store = await _store(session_factory)

    for name in ("Bash", "BashOutput", "KillShell"):
        verdict = classify(
            name,
            {"command": "env"},
            store,
            memory_dir="/memory",
            workspace_dir="/workspace",
        )
        assert verdict.decision is GateDecision.DENY, name
        assert "sandbox shell" in verdict.reason


async def test_extra_read_only_tool_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An MCP read tool the caller declared read-only (e.g. calendar free/busy) ALLOWs.
    store = await _store(session_factory)

    verdict = classify(
        "mcp__gcal__get-freebusy",
        {},
        store,
        extra_read_only=frozenset({"mcp__gcal__get-freebusy"}),
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_calendar_write_not_in_read_set_asks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A write tool absent from extra_read_only falls through to ASK → approval.
    store = await _store(session_factory)

    verdict = classify(
        "mcp__gcal__create-event",
        {},
        store,
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
        extra_read_only=frozenset({"mcp__gcal__get-freebusy"}),
    )

    assert verdict.decision is GateDecision.DENY


async def test_file_op_within_memory_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify(
        "Read", {"file_path": "/memory/facts/owner/x.md"}, store, memory_dir="/memory"
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_relative_file_op_resolves_under_memory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A relative path resolves against the session cwd (the memory root) → in-bounds.
    store = await _store(session_factory)

    verdict = classify(
        "Glob", {"path": "facts/owner"}, store, memory_dir="/memory"
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_file_op_outside_memory_denies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify(
        "Read", {"file_path": "/etc/shadow"}, store, memory_dir="/memory"
    )

    assert verdict.decision is GateDecision.DENY


async def test_file_op_traversal_escape_denies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify(
        "Read", {"file_path": "/memory/../etc/shadow"}, store, memory_dir="/memory"
    )

    assert verdict.decision is GateDecision.DENY


async def test_never_still_wins_over_memory_confinement(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory, never=[("Read", None)])

    verdict = classify(
        "Read", {"file_path": "/memory/x.md"}, store, memory_dir="/memory"
    )

    assert verdict.decision is GateDecision.DENY


# ---- workspace scoping (M7) --------------------------------------------------


async def test_read_in_workspace_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify(
        "Read",
        {"file_path": "/workspace/build/out.txt"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_read_in_memory_still_allows_with_workspace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Reads span memory ∪ workspace: a memory path is still in-bounds.
    store = await _store(session_factory)

    verdict = classify(
        "Grep",
        {"path": "/memory/facts/owner"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_read_outside_memory_and_workspace_denies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify(
        "Read",
        {"file_path": "/etc/shadow"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.DENY


async def test_write_in_workspace_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify(
        "Write",
        {"file_path": "/workspace/draft.md", "content": "x"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_edit_in_workspace_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify(
        "Edit",
        {"file_path": "/workspace/draft.md"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_write_into_memory_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Issue-19: memory writes are now allowed — the gate widens Write/Edit to memory ∪
    # workspace so the agent can persist facts without an approval card.
    store = await _store(session_factory)

    verdict = classify(
        "Write",
        {"file_path": "/memory/facts/owner/x.md", "content": "x"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_edit_into_memory_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory)

    verdict = classify(
        "Edit",
        {"file_path": "/memory/facts/owner/x.md"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_relative_write_resolves_to_memory_cwd_and_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A relative path resolves against the session cwd (the memory root) → inside
    # memory → now ALLOW (Issue-19 widened writes to memory ∪ workspace).
    store = await _store(session_factory)

    verdict = classify(
        "Write",
        {"file_path": "draft.md", "content": "x"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.ALLOW


async def test_write_outside_memory_and_workspace_denies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A write outside both memory and workspace is still a hard DENY.
    store = await _store(session_factory)

    verdict = classify(
        "Write",
        {"file_path": "/etc/shadow", "content": "x"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.DENY


async def test_write_guest_no_roots_denies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Guest sessions have no memory or workspace roots → any write is DENY.
    store = await _store(session_factory)

    verdict = classify(
        "Write",
        {"file_path": "/tmp/anything.md", "content": "x"},
        store,
    )

    assert verdict.decision is GateDecision.DENY


async def test_never_wins_over_memory_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A NEVER rule on Write still hard-denies, even for a memory path.
    store = await _store(session_factory, never=[("Write", None)])

    verdict = classify(
        "Write",
        {"file_path": "/memory/facts/x.md", "content": "y"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.DENY


async def test_never_wins_over_workspace_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(session_factory, never=[("Write", None)])

    verdict = classify(
        "Write",
        {"file_path": "/workspace/x", "content": "y"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.DENY


async def test_shell_tool_asks_by_default(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The sandbox shell tool is effectful → ASK unless pre-approved (like Bash).
    store = await _store(session_factory)

    verdict = classify(
        "mcp__chief_shell__bash",
        {"command": "ls /workspace"},
        store,
        memory_dir="/memory",
        workspace_dir="/workspace",
    )

    assert verdict.decision is GateDecision.ASK


async def test_shell_tool_approved_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _store(
        session_factory, approved=[("mcp__chief_shell__bash", "git status")]
    )

    verdict = classify(
        "mcp__chief_shell__bash", {"command": "git status"}, store
    )

    assert verdict.decision is GateDecision.ALLOW
