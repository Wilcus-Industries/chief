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

#: Owner-only calendar guidance (M5), included when the calendar is wired. The stand-in
#: for the packaged booking Skill that arrives with the skills framework (M10).
_CALENDAR_BOOKING_GUIDANCE = (
    "## Calendar\n"
    "You can read {owner}'s Google Calendar and propose, create, or update events. "
    "Only ever book a slot that is BOTH free on the calendar (check free/busy or list "
    "events first) AND within {owner}'s scheduling preferences in memory — never "
    "double-book. State every time in {owner}'s timezone ({tz}) and set that timezone "
    "explicitly when writing an event. Creating or updating an event needs {owner}'s "
    "approval, so propose the exact time and let the approval card confirm it."
)

#: Owner-only Drive guidance (M8), included when the drive server is wired.
_DRIVE_GUIDANCE = (
    "## Google Drive\n"
    "You can read {owner}'s Google Drive files (Docs, PDFs, Office files) from a Drive "
    "URL, and render a local Markdown file to PDF and upload it to a Drive folder. "
    "Reading is free; uploading a file needs {owner}'s approval."
)

#: Owner-only Sheets guidance (M8), included when the sheets server is wired.
_SHEETS_GUIDANCE = (
    "## Google Sheets\n"
    "You can read {owner}'s Google Sheets and propose edits. Reading ranges, formulas, "
    "and sheet/spreadsheet listings is free; writing cells, adding rows or sheets, and "
    "sharing a spreadsheet need {owner}'s approval. You can never edit row 1 (the "
    "header row) — it is blocked server-side."
)

#: service name → its owner guidance block. Appended in this order when each is enabled.
_SERVICE_GUIDANCE = {
    "calendar": _CALENDAR_BOOKING_GUIDANCE,
    "drive": _DRIVE_GUIDANCE,
    "sheets": _SHEETS_GUIDANCE,
}


def build_system_prompt(
    *,
    tier: str,
    memory: MemoryStore,
    owner_name: str,
    google_services: frozenset[str] = frozenset(),
    owner_tz: str | None = None,
) -> str:
    """Assemble the system prompt for a ``tier`` session against ``memory``.

    ``google_services`` names the wired Google MCP servers (owner only) — e.g.
    ``{"calendar", "drive"}``. Each contributes a guidance block so chief knows the
    service's rules (calendar booking states times in ``owner_tz``).
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
        tz = owner_tz or "the owner's timezone"
        for name, guidance in _SERVICE_GUIDANCE.items():
            if name in google_services:
                sections.append(guidance.format(owner=owner_name, tz=tz))
    else:
        sections = [memory.soul(), _GUEST_FRAMING.format(owner=owner_name)]
    return "\n\n".join(section.strip() for section in sections if section.strip())
