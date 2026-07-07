"""Opt-in live test: OpenRouter BYOK provider target class under CopilotBackend
(#90, part of #72).

Skipped unless ``CHIEF_COPILOT_LIVE`` is set **and** an OpenRouter key is available —
the manual "does a real OpenRouter BYOK turn actually work" check, not a CI test. The
key is read the production way, via :func:`~chief.core.copilot_session.
openrouter_provider_config` off a :class:`~chief.config.Settings` built from the local
``./secrets`` directory (mirrors :mod:`chief.app`'s Docker-secrets loading, pointed at
the host path instead of ``/run/secrets``) — never hardcoded, never printed.

Two central-mechanism checks, proven for real:

* ``test_live_openrouter_turn_serves_requested_model`` drives a full turn through
  :class:`~chief.core.backend.CopilotBackend` /
  :class:`~chief.core.copilot_session.CopilotTaskSession` — the same seam production
  code uses — and asserts the served model (event ``data.model``, captured as
  ``last_served_model``) matches the requested OpenRouter model.
* ``test_live_openrouter_tool_round_trips`` exercises the client ``@define_tool``
  round-trip. ``CopilotTaskSession.run_turn`` doesn't forward ``tools=`` yet (out of
  this slice's scope — a later #72 slice wires tool forwarding through the backend
  seam), so this drives ``copilot.CopilotClient`` directly with the same
  ``openrouter_provider_config(settings)`` production helper — the smaller-blast-radius
  option noted in #90, rather than widening ``CopilotTaskSession``'s production surface
  with a kwarg nothing else uses yet.

Run it:

1. Log in once with the Copilot CLI so the SDK can spawn an authenticated runtime
   (``copilot login``).
2. Put a working key in ``secrets/openrouter_api_key`` (get one at
   https://openrouter.ai/settings/keys).
3. Set ``CHIEF_COPILOT_LIVE=1``, then run::

       CHIEF_COPILOT_LIVE=1 uv run pytest tests/test_openrouter_live.py

``CHIEF_OPENROUTER_MODEL`` (default ``anthropic/claude-haiku-4.5``) overrides the
requested model.
"""

import os

import copilot
import pytest
from copilot.session_events import AssistantMessageData, AssistantUsageData
from pydantic import BaseModel, ValidationError

from chief.config import Settings
from chief.core.backend import CopilotBackend
from chief.core.copilot_session import CopilotTaskSession, openrouter_provider_config
from chief.core.session import Final


def _load_local_settings() -> Settings | None:
    """Build ``Settings`` from ``./secrets`` (the host path, not ``/run/secrets``).

    ``None`` when the local environment doesn't have a full, valid config (e.g. no
    chat platform configured) — the skip gate below treats that the same as "no key".
    """
    try:
        return Settings(_secrets_dir="secrets")  # type: ignore[call-arg]
    except ValidationError:
        return None


_settings = _load_local_settings()
_MODEL = os.environ.get("CHIEF_OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")

pytestmark = pytest.mark.skipif(
    not os.environ.get("CHIEF_COPILOT_LIVE")
    or _settings is None
    or not _settings.openrouter_api_key,
    reason=(
        "live OpenRouter test — set CHIEF_COPILOT_LIVE=1 and populate "
        "secrets/openrouter_api_key (needs a Copilot login too)"
    ),
)


@pytest.mark.timeout(120)  # live runtime spawn + model round-trip; override 30s cap
async def test_live_openrouter_turn_serves_requested_model() -> None:
    assert _settings is not None  # narrowed by the module-level skip gate
    provider = openrouter_provider_config(_settings)
    backend = CopilotBackend()
    session = backend.create_session(model=_MODEL, provider=provider)
    assert isinstance(session, CopilotTaskSession)  # narrows past SessionProto's slice
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
    assert session.last_served_model == _MODEL, (
        f"expected the openrouter target to serve {_MODEL!r}, got "
        f"{session.last_served_model!r}"
    )


@pytest.mark.timeout(120)
async def test_live_openrouter_tool_round_trips() -> None:
    assert _settings is not None
    provider = openrouter_provider_config(_settings)
    invoked: dict[str, str] = {}

    class NoteParams(BaseModel):
        note: str

    @copilot.define_tool(description="Record a note and confirm receipt")
    def record_note(params: NoteParams) -> str:
        invoked["note"] = params.note
        return f"recorded: {params.note}"

    served_models: list[str] = []

    def _on_event(event: copilot.session_events.SessionEvent) -> None:
        data = event.data
        if isinstance(data, (AssistantMessageData, AssistantUsageData)):
            if data.model:
                served_models.append(data.model)

    client = copilot.CopilotClient()
    await client.start()
    try:
        session = await client.create_session(
            model=_MODEL, provider=provider, tools=[record_note]
        )
        session.on(_on_event)
        await session.send_and_wait(
            "Call the record_note tool with note set to the exact string 'pong', "
            "then reply with the word done."
        )
    finally:
        await client.stop()

    assert invoked.get("note") == "pong", "the record_note tool was never invoked"
    assert served_models and served_models[-1] == _MODEL
