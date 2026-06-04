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

#: Owner-only calendar guidance (M5), included when the calendar is wired. The M5
#: stand-in for the packaged booking Skill that arrives with the skills framework (M10).
_CALENDAR_BOOKING_GUIDANCE = (
    "## Calendar\n"
    "You can read {owner}'s Google Calendar and propose, create, or update events. "
    "Only ever book a slot that is BOTH free on the calendar (check free/busy or list "
    "events first) AND within {owner}'s scheduling preferences in memory — never "
    "double-book. State every time in {owner}'s timezone ({tz}) and set that timezone "
    "explicitly when writing an event. Creating or updating an event needs {owner}'s "
    "approval, so propose the exact time and let the approval card confirm it."
)


def build_system_prompt(
    *,
    tier: str,
    memory: MemoryStore,
    owner_name: str,
    calendar_enabled: bool = False,
    owner_tz: str | None = None,
) -> str:
    """Assemble the system prompt for a ``tier`` session against ``memory``.

    When ``calendar_enabled`` (owner only, M5), the booking guidance is appended so
    chief books only free, in-preference slots and states times in ``owner_tz``.
    """
    if tier == "owner":
        sections = [
            memory.soul(),
            _OWNER_FRAMING,
            memory.user(),
            _RECALL_HINT,
            "## Memory index",
            memory.index(),
        ]
        if calendar_enabled:
            sections.append(
                _CALENDAR_BOOKING_GUIDANCE.format(
                    owner=owner_name, tz=owner_tz or "the owner's timezone"
                )
            )
    else:
        sections = [memory.soul(), _GUEST_FRAMING.format(owner=owner_name)]
    return "\n\n".join(section.strip() for section in sections if section.strip())
