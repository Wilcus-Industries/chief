"""Google multi-account selection precedence (issue #49).

Decides WHAT the active account is when a thread has no explicit binding:

1. Per-thread binding already exists  →  use it (handled by the caller; if label is
   already set this module is not consulted).
2. Long-term memory carries a hint (an email matching a registered account appears in
   ``User.md`` or any ``facts/owner/`` file)  →  auto-select that account and persist it
   as the thread binding so the next call skips the lookup.
3. No hint, multiple registered accounts  →  inject :data:`ACCOUNT_SELECTION_GUIDANCE`
   into the system prompt so the model asks the owner which account to use before
   calling Google APIs.  The model then calls ``set_account``, binding the thread for
   all future calls.

When only one account is registered there is nothing to choose: the caller passes no
extra guidance and the server falls back to the single credential.  When no accounts are
registered at all the guidance is also omitted — there is nothing to select from.
"""

import re
from typing import TYPE_CHECKING

from .accounts import GoogleAccount

if TYPE_CHECKING:
    from ...memory.store import MemoryStore

#: Injected into the owner system prompt when the thread has no active account AND
#: memory carries no hint AND there are multiple registered accounts.  Instructs the
#: model to ask the owner which Google account to use before touching any Google API.
ACCOUNT_SELECTION_GUIDANCE = (
    "## Google account selection\n"
    "This thread has no active Google account set. Before calling any Google API "
    "(Calendar, Drive, Sheets, Gmail), ask the owner which Google account to use, "
    "then call set_account to bind it for this thread. "
    "Do NOT guess or use the first account silently."
)

#: Simple pattern to extract email-shaped strings from free text.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def extract_account_hint(
    memory: "MemoryStore",
    accounts: list[GoogleAccount],
) -> str | None:
    """Return the label of the first registered account whose email appears in memory.

    Scans ``User.md`` and every ``facts/owner/`` fact body for email-shaped strings,
    then returns the label of the first :class:`GoogleAccount` whose ``email`` or
    ``label`` matches one of the found addresses.  Returns ``None`` when no match is
    found (no hint in memory) or when ``accounts`` is empty.

    The scan is case-insensitive for the email address itself (RFC 5321 local-parts
    are case-sensitive in theory but all major providers treat them as case-insensitive
    in practice; labels/emails in the registry are already canonical from discovery).
    """
    if not accounts:
        return None

    # Build a lower-cased set of emails/labels to match against.
    label_by_email: dict[str, str] = {}
    for acct in accounts:
        if acct.email:
            label_by_email[acct.email.lower()] = acct.label
        # also index by label in case label != email (legacy slug accounts)
        label_by_email[acct.label.lower()] = acct.label

    # Collect all email-shaped strings from User.md
    candidates = set(_EMAIL_RE.findall(memory.user()))

    # Collect from every owner fact body
    for fact in memory.list_facts("owner"):
        candidates.update(_EMAIL_RE.findall(fact.body))

    for addr in candidates:
        match = label_by_email.get(addr.lower())
        if match is not None:
            return match

    return None
