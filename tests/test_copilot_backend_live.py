"""Opt-in live tests: real turns through CopilotBackend (#76 / #78, part of #72).

Skipped unless ``CHIEF_COPILOT_LIVE`` is set — the manual "does a real Copilot-SDK turn
actually return a reply through the backend" check, not a CI test. It spawns the real
Copilot runtime, opens a streaming session on a Copilot-quota model, sends one owner
text message, and asserts a non-empty ``Final`` streamed back with the turn ending
cleanly. A second test (#78) installs chief's persona via the ``customize``-mode
system message and checks a real reply for vendor-identity leakage — the one thing
the mocked ``tests/test_copilot_session.py`` suite cannot prove.

Auth comes from the logged-in GitHub Copilot user (the spike's ``copilot`` CLI login) —
no token is passed. On the Student plan model choice is ``auto`` regardless of the name
sent, so the served model is whatever GitHub picks.

Run it:

1. Log in once with the Copilot CLI so the SDK can spawn an authenticated runtime.
2. Download the pinned runtime (or let the SDK fetch it on first use)::

       uv run python -m copilot download-runtime

3. Set ``CHIEF_COPILOT_LIVE=1``, then run::

       CHIEF_COPILOT_LIVE=1 uv run pytest tests/test_copilot_backend_live.py

``CHIEF_COPILOT_MODEL`` (default ``auto``) overrides the requested model.
"""

import os

import pytest

from chief.core.backend import CopilotBackend
from chief.core.copilot_session import find_vendor_identity_leak
from chief.core.session import Final, Milestone

pytestmark = pytest.mark.skipif(
    not os.environ.get("CHIEF_COPILOT_LIVE"),
    reason="live Copilot test — set CHIEF_COPILOT_LIVE=1 (needs a Copilot login)",
)


@pytest.mark.timeout(120)  # live runtime spawn + model round-trip; override 30s cap
async def test_live_owner_turn_returns_reply() -> None:
    model = os.environ.get("CHIEF_COPILOT_MODEL", "auto")
    backend = CopilotBackend()
    session = backend.create_session(model=model)
    try:
        events = [
            event
            async for event in session.run_turn(
                "Reply with exactly the word: pong"
            )
        ]
    finally:
        await session.aclose()

    finals = [e.text for e in events if isinstance(e, Final)]
    assert finals, f"expected at least one Final reply, got {events!r}"
    assert any(text.strip() for text in finals), "reply text was empty"
    # Milestones (if any) precede the reply; the turn ended cleanly (run_turn returned).
    assert all(isinstance(e, (Final, Milestone)) for e in events)
    assert session.session_id, "a resumable session id should have been captured"


@pytest.mark.timeout(120)  # live runtime spawn + model round-trip; override 30s cap
async def test_live_persona_turn_has_no_vendor_identity_leak() -> None:
    """#78's live probe: a real turn with chief's persona installed via the
    ``customize``-mode system message, asked point-blank who it is. Everything the
    mocked tests can't prove — whether the real Copilot runtime actually honors the
    section overrides — is exercised here. Not run in CI (opt-in, needs a login).
    """
    model = os.environ.get("CHIEF_COPILOT_MODEL", "auto")
    persona = (
        "I am chief, a personal AI assistant for my owner. I never mention GitHub "
        "Copilot, Codex, or any other vendor name — I speak only as chief."
    )
    backend = CopilotBackend()
    session = backend.create_session(model=model, system_prompt=persona)
    try:
        events = [
            event
            async for event in session.run_turn(
                "In one sentence, who are you and who made you?"
            )
        ]
    finally:
        await session.aclose()

    finals = [e.text for e in events if isinstance(e, Final)]
    assert finals, f"expected at least one Final reply, got {events!r}"
    leaks = [(text, find_vendor_identity_leak(text)) for text in finals]
    assert all(leak is None for _text, leak in leaks), (
        f"vendor identity leaked through the persona customization: {leaks!r}"
    )
