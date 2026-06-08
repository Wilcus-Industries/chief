"""System-prompt assembly per tier (DESIGN: Soul.md + tier framing + memory).

``build_system_prompt`` composes a session's system prompt from ``Soul.md`` (chief's
identity), a tier-specific framing, and — for the owner — ``User.md`` plus the
``MEMORY.md`` index plus the memory-writing guidance block (when/where/how to
persist durable facts). The prompt is fixed at session construction; a mid-session
distill updates ``MEMORY.md`` for the *next* session, and chief can Write to
``User.md`` or the ``facts/`` tree in the same session via its memory-confined Write
tool.

Tier isolation is by construction (DESIGN): a guest prompt never carries the owner's
``User.md``, memory index, or memory-writing guidance, and guest sessions are wired
with no owner tools at all.
"""

from collections.abc import Sequence

from ..memory.store import MemoryStore
from ..tools.shell import SANDBOX_SHELL_CONTRACT

_OWNER_FRAMING = (
    "You are operating for your owner directly. You are a capable, trusted operator: "
    "act decisively within the permission gate, surface uncertainty plainly, and keep "
    "answers concise."
)

_GUEST_FRAMING = (
    "You are {owner}'s assistant, acting as a receptionist for someone who is not "
    "{owner}. Be polite and helpful within a narrow scope: take a message, check "
    "availability, or request a booking. Never reveal {owner}'s private information. "
    "Anything outside taking a message or helping find a time, decline politely."
)

#: Guest-only scheduling guidance (M6), included when the narrowed calendar is wired.
#: Deliberately narrower than the owner's calendar block: free/busy only (never event
#: details), and booking is a proposal the owner approves — never a direct write.
_GUEST_CALENDAR_GUIDANCE = (
    "## Scheduling\n"
    "You can check {owner}'s availability (free/busy only — you never see what the "
    "events are) and propose a booking. State every time in {owner}'s timezone ({tz}). "
    "Only ever propose a slot that is free. Creating the booking needs {owner}'s "
    "approval, so propose the exact time, let the approval card confirm it, and tell "
    "the visitor you've passed the request along."
)

#: Owner-only guidance (M6), included when the guest-admin tool is wired.
_GUEST_ADMIN_GUIDANCE = (
    "## Managing guests\n"
    "Visitors who aren't {owner} reach you as a receptionist. If {owner} asks you to "
    "block, mute, or unblock a guest by name, use the manage_guest tool."
)

_MEMORY_GUIDANCE = (
    "## Memory\n"
    "You have a persistent memory you can read and write freely.\n\n"
    "**Write to memory when:** {owner} shares a durable preference, a meaningful\n"
    "personal fact, an ongoing commitment, or anything they would want you to\n"
    "remember next session.  Do *not* write ephemeral chatter or one-off task\n"
    "details — only what has lasting value.\n\n"
    "**Where to write:**\n"
    "- `User.md` (the `## Preferences` section for standing preferences; the\n"
    "  `## Facts` section for profile details and background knowledge) — use the\n"
    "  Write or Edit tool to update it directly.\n"
    "- A standalone `facts/` file only for time-bound or genuinely self-contained\n"
    "  notes that don't belong in the profile.\n\n"
    "**Format:** plain Markdown, concise, one idea per bullet or short paragraph.\n"
    "Update `MEMORY.md` if you add a new standalone fact file (one pointer line per\n"
    "file).\n\n"
    "To *recall*, Read any file whose index line looks relevant."
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

#: Owner-only Gmail guidance (M8), included when the gmail server is wired.
_GMAIL_GUIDANCE = (
    "## Gmail\n"
    "You can read, search, and triage {owner}'s Gmail, and send mail, reply, draft, "
    "label, and trash messages on their behalf. Reading and searching is free; "
    "sending, replying, drafting, labelling, and trashing need {owner}'s approval. "
    "Every message you send carries a transparent line noting an assistant sent it "
    "(appended server-side — you don't write it). You cannot permanently delete "
    "drafts or labels."
)

#: service name → its owner guidance block. Appended in this order when each is enabled.
_SERVICE_GUIDANCE = {
    "calendar": _CALENDAR_BOOKING_GUIDANCE,
    "drive": _DRIVE_GUIDANCE,
    "sheets": _SHEETS_GUIDANCE,
    "gmail": _GMAIL_GUIDANCE,
}

#: Owner-only web guidance (M7). Web search/fetch are always wired for the owner.
_WEB_GUIDANCE = (
    "## Web\n"
    "You can search the web (WebSearch) and fetch a specific URL (WebFetch). Both are "
    "read-only and need no approval — use them freely to look things up, then cite "
    "what you found. WebFetch only performs GET requests."
)

#: Owner-only workspace guidance (M7), included when the workspace is enabled.
_WORKSPACE_GUIDANCE = (
    "## Workspace\n"
    "/workspace is a scratch directory you can Read, Write, and Edit freely with no "
    "approval — it is shared with your shell (the shell's working directory is "
    "/workspace). Keep working files there. You can write to memory (User.md and the "
    "facts/ tree) and to /workspace; writes outside memory ∪ workspace are blocked."
)

#: Owner-only skills guidance (M10), included when packaged skills are enabled. Names
#: the available skills so chief reaches for a packaged workflow instead of improvising.
_SKILLS_GUIDANCE = (
    "## Skills\n"
    "You have packaged skills — self-contained expert workflows you invoke with the "
    "Skill tool when a request matches one. Prefer a matching skill over improvising. "
    "Available: {skills}."
)

#: Owner-only shell guidance (M7), included when the sandbox shell is enabled.
_SHELL_GUIDANCE = (
    "## Shell\n"
    "You can run bash commands in a sandboxed Linux container (the bash tool). Its "
    "working directory is /workspace and it has internet access (pip, git, curl all "
    "work). Shell state — environment variables, the current directory, background "
    "jobs — persists across commands within a task, but not across a restart. "
    f"{SANDBOX_SHELL_CONTRACT} Running a command needs your owner's approval unless "
    "they have pre-approved it, so prefer one clear command and let the approval card "
    "confirm it."
)


def build_system_prompt(
    *,
    tier: str,
    memory: MemoryStore,
    owner_name: str,
    google_services: frozenset[str] = frozenset(),
    owner_tz: str | None = None,
    workspace_enabled: bool = False,
    shell_enabled: bool = False,
    guest_admin_enabled: bool = False,
    skills: Sequence[str] = (),
) -> str:
    """Assemble the system prompt for a ``tier`` session against ``memory``.

    ``google_services`` names the wired Google MCP servers (owner only) — e.g.
    ``{"calendar", "drive"}``. Each contributes a guidance block so chief knows the
    service's rules (calendar booking states times in ``owner_tz``).
    ``workspace_enabled`` / ``shell_enabled`` add the M7 workspace + sandbox-shell
    guidance; the web block is always present for the owner (web tools are always
    wired). ``skills`` (M10, owner only) names the enabled packaged skills, adding a
    block that points chief at them. Guests get none of these.
    """
    if tier == "owner":
        sections = [
            memory.soul(),
            _OWNER_FRAMING,
            memory.user(),
            _MEMORY_GUIDANCE.format(owner=owner_name),
            "## Memory index",
            memory.index(),
        ]
        tz = owner_tz or "the owner's timezone"
        for name, guidance in _SERVICE_GUIDANCE.items():
            if name in google_services:
                sections.append(guidance.format(owner=owner_name, tz=tz))
        sections.append(_WEB_GUIDANCE)
        if workspace_enabled:
            sections.append(_WORKSPACE_GUIDANCE)
        if shell_enabled:
            sections.append(_SHELL_GUIDANCE)
        if skills:
            sections.append(_SKILLS_GUIDANCE.format(skills=", ".join(skills)))
        if guest_admin_enabled:
            sections.append(_GUEST_ADMIN_GUIDANCE.format(owner=owner_name))
    else:
        sections = [memory.soul(), _GUEST_FRAMING.format(owner=owner_name)]
        if "calendar" in google_services:
            tz = owner_tz or "the owner's timezone"
            sections.append(
                _GUEST_CALENDAR_GUIDANCE.format(owner=owner_name, tz=tz)
            )
    return "\n\n".join(section.strip() for section in sections if section.strip())
