"""Memory interface — the namespaced store contract + its value types.

Defined as a :class:`Protocol` so the markdown backend (M4) and a future semantic/graph
backend (mem0, per DESIGN) are interchangeable without touching callers. Readers
(:meth:`index`/:meth:`soul`/:meth:`user`/:meth:`list_facts`) are synchronous file reads
used while assembling a system prompt; mutators (:meth:`forget`/:meth:`purge_expired`/
:meth:`ensure_scaffold`) are async because each commits through the versioner.

Namespaces partition memory by subject: ``"owner"`` for the owner's own facts and
``"contacts/<id>"`` for a guest's (the contact namespace is exercised in M6).
"""

from dataclasses import dataclass
from typing import Protocol

OWNER_NAMESPACE = "owner"


@dataclass(frozen=True)
class Fact:
    """A single stored fact — one markdown file under ``facts/<namespace>/<slug>.md``.

    ``provenance`` records origin (``owner-stated`` / ``inferred`` / ``guest-stated``),
    ``trust`` its reliability, and ``expires`` an optional ISO-8601 instant after which
    :meth:`MemoryStore.purge_expired` drops it (DESIGN: flag time-sensitive facts).
    """

    slug: str
    title: str
    body: str
    namespace: str
    provenance: str
    trust: str
    expires: str | None
    created: str


class MemoryStore(Protocol):
    """Namespaced long-term memory (DESIGN: memory interface)."""

    def index(self) -> str:
        """The ``MEMORY.md`` index text, loaded into every system prompt."""
        ...

    def soul(self) -> str:
        """``Soul.md`` — chief's identity/voice."""
        ...

    def user(self) -> str:
        """``User.md`` — the owner's profile/preferences."""
        ...

    def list_facts(self, namespace: str) -> list[Fact]:
        """Every fact in ``namespace`` (for ``/memory`` and inspection)."""
        ...

    async def forget(self, namespace: str, query: str) -> list[Fact]:
        """Remove facts in ``namespace`` matching ``query``; return what was removed."""
        ...

    async def purge_expired(self) -> int:
        """Drop every TTL-expired fact across namespaces; return the count removed."""
        ...

    async def ensure_scaffold(self) -> None:
        """Seed ``Soul.md``/``User.md``/``MEMORY.md`` + ``facts/owner/`` if absent."""
        ...
