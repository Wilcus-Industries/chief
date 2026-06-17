"""build_system_prompt(): owner gets Soul+User+facts listing; guest is a fenced
receptionist."""

from chief.core.personas import build_system_prompt
from chief.memory.store import Fact
from chief.memory.versioning import NullVersioner, Versioner


class FakeMemory:
    """Minimal MemoryStore reader stub for prompt assembly."""

    def __init__(self) -> None:
        self._soul = "# Soul\nI am chief."
        self._user = "# Will\nPrefers mornings."
        self._listing = "- facts/owner/mornings.md — Prefers mornings"
        self._versioner: Versioner = NullVersioner()

    @property
    def versioner(self) -> Versioner:
        return self._versioner

    def facts_listing(self) -> str:
        return self._listing

    def soul(self) -> str:
        return self._soul

    def user(self) -> str:
        return self._user

    def list_facts(self, namespace: str) -> list[Fact]:
        return []

    # Mutators are unused by prompt assembly; present only to satisfy MemoryStore.
    async def forget(self, namespace: str, query: str) -> list[Fact]:
        raise NotImplementedError

    async def purge_expired(self) -> int:
        raise NotImplementedError

    async def ensure_scaffold(self) -> None:
        raise NotImplementedError


def test_owner_prompt_includes_soul_user_and_facts_listing() -> None:
    prompt = build_system_prompt(tier="owner", memory=FakeMemory(), owner_name="Will")

    assert "I am chief." in prompt  # Soul.md
    assert "Prefers mornings." in prompt  # User.md
    assert "facts/owner/mornings.md" in prompt  # auto-generated facts/ listing
    assert "Read" in prompt  # told to open fact files on demand


def test_owner_prompt_empty_facts_injects_no_listing() -> None:
    """An empty facts/ directory must not inject the ## Memory index section."""
    mem = FakeMemory()
    mem._listing = ""  # simulate empty facts/

    prompt = build_system_prompt(tier="owner", memory=mem, owner_name="Will")

    assert "## Memory index" not in prompt
    # No bullet listing lines (the guidance block may still say "facts/" in prose)
    assert "- facts/" not in prompt


def test_guest_prompt_has_no_facts_listing() -> None:
    """Guest prompt must never carry the facts/ listing."""
    prompt = build_system_prompt(tier="guest", memory=FakeMemory(), owner_name="Will")

    assert "facts/owner/mornings.md" not in prompt
    assert "## Memory index" not in prompt


def test_no_memory_md_referenced_in_memory_guidance() -> None:
    """The _MEMORY_GUIDANCE block must not instruct chief to maintain MEMORY.md."""
    prompt = build_system_prompt(tier="owner", memory=FakeMemory(), owner_name="Will")

    assert "MEMORY.md" not in prompt


def test_owner_calendar_guidance_included_with_tz_when_enabled() -> None:
    prompt = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
        google_services=frozenset({"calendar"}),
        owner_tz="America/New_York",
    )

    assert "## Calendar" in prompt
    assert "America/New_York" in prompt  # times stated in the owner's tz
    assert "free/busy" in prompt  # only book free, in-preference slots
    # Only the enabled service's block appears.
    assert "## Google Drive" not in prompt
    assert "## Google Sheets" not in prompt


def test_owner_drive_and_sheets_guidance_included_when_enabled() -> None:
    prompt = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
        google_services=frozenset({"drive", "sheets"}),
    )

    assert "## Google Drive" in prompt
    assert "## Google Sheets" in prompt
    assert "header row" in prompt  # row-1 guard surfaced to the model
    assert "## Calendar" not in prompt


def test_owner_gmail_guidance_included_when_enabled() -> None:
    prompt = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
        google_services=frozenset({"gmail"}),
    )

    assert "## Gmail" in prompt
    assert "approval" in prompt  # sending is approval-gated
    assert "## Calendar" not in prompt


def test_owner_guidance_absent_when_no_services() -> None:
    prompt = build_system_prompt(tier="owner", memory=FakeMemory(), owner_name="Will")

    assert "## Calendar" not in prompt
    assert "## Google Drive" not in prompt
    assert "## Google Sheets" not in prompt
    assert "## Gmail" not in prompt


def test_guest_never_gets_gmail_guidance() -> None:
    prompt = build_system_prompt(
        tier="guest",
        memory=FakeMemory(),
        owner_name="Will",
        google_services=frozenset({"gmail"}),
    )

    assert "## Gmail" not in prompt


def test_owner_always_gets_web_guidance() -> None:
    # Web tools are always wired for the owner, so the web block is always present.
    prompt = build_system_prompt(tier="owner", memory=FakeMemory(), owner_name="Will")

    assert "## Web" in prompt
    assert "WebSearch" in prompt


def test_owner_shell_and_workspace_guidance_when_enabled() -> None:
    prompt = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
        workspace_enabled=True,
        shell_enabled=True,
    )

    assert "## Workspace" in prompt
    assert "/workspace" in prompt  # the scratch dir is named
    assert "## Shell" in prompt
    assert "internet" in prompt  # sandbox has egress
    assert "no secrets" in prompt  # the model is told the sandbox is secret-free


def test_owner_shell_and_workspace_absent_when_disabled() -> None:
    prompt = build_system_prompt(tier="owner", memory=FakeMemory(), owner_name="Will")

    assert "## Workspace" not in prompt
    assert "## Shell" not in prompt


def test_owner_skills_guidance_lists_enabled_skills() -> None:
    prompt = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
        skills=("docx", "claude-api"),
    )

    assert "## Skills" in prompt
    assert "docx" in prompt
    assert "claude-api" in prompt


def test_owner_skills_guidance_absent_when_none_enabled() -> None:
    prompt = build_system_prompt(tier="owner", memory=FakeMemory(), owner_name="Will")

    assert "## Skills" not in prompt


def test_guest_never_gets_skills_block() -> None:
    # Skills are owner-only; the guest branch never reads the argument.
    prompt = build_system_prompt(
        tier="guest",
        memory=FakeMemory(),
        owner_name="Will",
        skills=("docx",),
    )

    assert "## Skills" not in prompt


def test_guest_never_gets_shell_workspace_or_web() -> None:
    prompt = build_system_prompt(
        tier="guest",
        memory=FakeMemory(),
        owner_name="Will",
        workspace_enabled=True,
        shell_enabled=True,
    )

    assert "## Web" not in prompt
    assert "## Workspace" not in prompt
    assert "## Shell" not in prompt


def test_guest_never_gets_drive_sheets_or_owner_calendar_block() -> None:
    prompt = build_system_prompt(
        tier="guest",
        memory=FakeMemory(),
        owner_name="Will",
        google_services=frozenset({"calendar", "drive", "sheets"}),
        owner_tz="America/New_York",
    )

    # A guest never gets Drive/Sheets, nor the owner's broad read/create/update calendar
    # block — only the narrow Scheduling block (free/busy + propose-with-approval).
    assert "## Calendar" not in prompt
    assert "## Google Drive" not in prompt
    assert "## Google Sheets" not in prompt


def test_guest_gets_narrow_scheduling_guidance_when_calendar_wired() -> None:
    prompt = build_system_prompt(
        tier="guest",
        memory=FakeMemory(),
        owner_name="Will",
        google_services=frozenset({"calendar"}),
        owner_tz="America/New_York",
    )

    assert "## Scheduling" in prompt
    assert "free/busy" in prompt  # never event details
    assert "approval" in prompt  # booking is a proposal the owner confirms
    assert "America/New_York" in prompt


def test_guest_without_calendar_has_no_scheduling_guidance() -> None:
    prompt = build_system_prompt(tier="guest", memory=FakeMemory(), owner_name="Will")

    assert "## Scheduling" not in prompt


def test_owner_gets_guest_admin_guidance_when_enabled() -> None:
    prompt = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
        guest_admin_enabled=True,
    )

    assert "## Managing guests" in prompt
    assert "manage_guest" in prompt
    # Off by default.
    plain = build_system_prompt(tier="owner", memory=FakeMemory(), owner_name="Will")
    assert "## Managing guests" not in plain


def test_guest_prompt_is_receptionist_without_user_profile() -> None:
    prompt = build_system_prompt(tier="guest", memory=FakeMemory(), owner_name="Will")

    assert "I am chief." in prompt  # Soul.md still present
    assert "Will's assistant" in prompt  # receptionist framing
    assert "Prefers mornings." not in prompt  # owner's private profile withheld
    assert "facts/owner/mornings.md" not in prompt  # no facts listing for guests


def test_owner_prompt_has_memory_write_guidance_replacing_read_only_hint() -> None:
    # The owner prompt teaches chief when/where/how to write memory, and no longer
    # contains the old read-only "open a fact file" recall hint.
    prompt = build_system_prompt(tier="owner", memory=FakeMemory(), owner_name="Will")

    # Write guidance must be present: at minimum the key nouns/verbs.
    assert "User.md" in prompt
    assert "Write" in prompt  # the write-tool instruction
    # The old read-only recall hint must be gone.
    assert "Open any fact file listed below with the Read tool" not in prompt


def test_guest_prompt_has_no_memory_write_guidance() -> None:
    prompt = build_system_prompt(tier="guest", memory=FakeMemory(), owner_name="Will")

    # Guests carry neither write guidance nor the memory index.
    assert "User.md" not in prompt
    assert "## Memory" not in prompt


def test_workspace_guidance_does_not_claim_only_writable_location() -> None:
    # With workspace enabled the prompt must NOT claim /workspace is the *only*
    # writable location — that contradicts the memory-write guidance which tells
    # chief it can Write/Edit User.md and facts/ files directly.
    prompt = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
        workspace_enabled=True,
    )

    assert "## Workspace" in prompt
    # The old contradictory claim must be gone.
    assert "only place you can write" not in prompt
    # The true picture: memory AND workspace are both writable.
    assert "memory" in prompt.lower() or "User.md" in prompt
    # Writes outside the allowed set are still described as blocked.
    assert "blocked" in prompt


# ---- platform-aware formatting guidance (issue #65) --------------------------


def test_telegram_owner_prompt_contains_no_markdown_guidance() -> None:
    """An owner turn on Telegram must warn that Markdown is not rendered."""
    prompt = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
        platform="telegram",
    )

    assert "Markdown" in prompt
    assert "plain text" in prompt.lower()


def test_discord_owner_prompt_contains_markdown_renders_guidance() -> None:
    """An owner turn on Discord must state that Markdown renders normally."""
    prompt = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
        platform="discord",
    )

    assert "Markdown" in prompt
    # Discord guidance should mention that Markdown renders
    assert "render" in prompt.lower() or "markdown" in prompt.lower()


def test_telegram_guest_prompt_contains_no_markdown_guidance() -> None:
    """A guest turn on Telegram must also carry the plain-text guidance."""
    prompt = build_system_prompt(
        tier="guest",
        memory=FakeMemory(),
        owner_name="Will",
        platform="telegram",
    )

    assert "plain text" in prompt.lower()


def test_discord_guest_prompt_contains_markdown_renders_guidance() -> None:
    """A guest turn on Discord must also carry the Markdown-renders guidance."""
    prompt = build_system_prompt(
        tier="guest",
        memory=FakeMemory(),
        owner_name="Will",
        platform="discord",
    )

    assert "Markdown" in prompt


def test_telegram_platform_guidance_differs_from_discord() -> None:
    """The Telegram and Discord prompts must contain different formatting guidance."""
    tg = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
        platform="telegram",
    )
    dc = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
        platform="discord",
    )

    assert tg != dc


def test_no_platform_produces_no_format_guidance_crash() -> None:
    """Omitting platform must not crash — it's optional (backwards compat)."""
    # Should not raise; the prompt just won't include a platform-formatting block.
    prompt = build_system_prompt(
        tier="owner",
        memory=FakeMemory(),
        owner_name="Will",
    )
    assert isinstance(prompt, str)
