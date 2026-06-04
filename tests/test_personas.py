"""build_system_prompt(): owner gets Soul+User+index; guest is a fenced receptionist."""

from chief.core.personas import build_system_prompt
from chief.memory.store import Fact


class FakeMemory:
    """Minimal MemoryStore reader stub for prompt assembly."""

    def __init__(self) -> None:
        self._soul = "# Soul\nI am chief."
        self._user = "# Will\nPrefers mornings."
        self._index = "- facts/owner/mornings.md — Prefers mornings"

    def index(self) -> str:
        return self._index

    def soul(self) -> str:
        return self._soul

    def user(self) -> str:
        return self._user

    def list_facts(self, namespace: str) -> list[Fact]:
        return []

    # Mutators are unused by prompt assembly; present only to satisfy MemoryStore.
    async def write_fact(self, **kwargs: object) -> Fact:
        raise NotImplementedError

    async def forget(self, namespace: str, query: str) -> list[Fact]:
        raise NotImplementedError

    async def purge_expired(self) -> int:
        raise NotImplementedError

    async def ensure_scaffold(self) -> None:
        raise NotImplementedError


def test_owner_prompt_includes_soul_user_and_index() -> None:
    prompt = build_system_prompt(tier="owner", memory=FakeMemory(), owner_name="Will")

    assert "I am chief." in prompt  # Soul.md
    assert "Prefers mornings." in prompt  # User.md
    assert "facts/owner/mornings.md" in prompt  # MEMORY.md index
    assert "Read" in prompt  # told to open fact files on demand


def test_guest_prompt_is_receptionist_without_user_profile() -> None:
    prompt = build_system_prompt(tier="guest", memory=FakeMemory(), owner_name="Will")

    assert "I am chief." in prompt  # Soul.md still present
    assert "Will's assistant" in prompt  # receptionist framing
    assert "Prefers mornings." not in prompt  # owner's private profile withheld
    assert "facts/owner/mornings.md" not in prompt  # no memory index for guests
