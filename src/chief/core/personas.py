"""System-prompt assembly per tier (DESIGN: Soul.md + tier framing + memory).

``build_system_prompt`` composes a session's system prompt from ``Soul.md`` (chief's
identity), a tier-specific framing, and — for the owner — ``User.md`` plus the
``MEMORY.md`` index with an instruction to open fact files on demand. The prompt is
fixed at session construction; a mid-session distill updates ``MEMORY.md`` for the
*next* session, though chief can always Read the current file via its memory-confined
file tools.

Tier isolation is by construction (DESIGN): a guest prompt never carries the owner's
``User.md`` or memory index, and guest sessions are wired with no owner tools at all.
"""

from ..memory.store import MemoryStore

_OWNER_FRAMING = (
    "You are operating for your owner directly. You are a capable, trusted operator: "
    "act decisively within the permission gate, surface uncertainty plainly, and keep "
    "answers concise."
)

_GUEST_FRAMING = (
    "You are {owner}'s assistant, acting as a receptionist for someone who is not "
    "{owner}. Be polite and helpful within a narrow scope: take a message, check "
    "availability, or request a booking. Never reveal {owner}'s private information."
)

_RECALL_HINT = (
    "Open any fact file listed below with the Read tool when its line looks relevant."
)


def build_system_prompt(
    *, tier: str, memory: MemoryStore, owner_name: str
) -> str:
    """Assemble the system prompt for a ``tier`` session against ``memory``."""
    if tier == "owner":
        sections = [
            memory.soul(),
            _OWNER_FRAMING,
            memory.user(),
            _RECALL_HINT,
            "## Memory index",
            memory.index(),
        ]
    else:
        sections = [memory.soul(), _GUEST_FRAMING.format(owner=owner_name)]
    return "\n\n".join(section.strip() for section in sections if section.strip())
