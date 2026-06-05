"""run_turn media path (Part C, M8): the streaming user envelope content blocks.

The string path stays a plain ``query(str)``; with attachments, ``run_turn`` must send a
one-item async-iterable of the Anthropic-shaped user envelope (text block + image/
document blocks) — the only way to carry media into ``ClaudeSDKClient.query``.
"""

import base64
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

from chief.adapters.base import Attachment
from chief.core.session import Final, TaskSession, _build_user_message


def test_build_user_message_text_only_is_single_text_block() -> None:
    env = _build_user_message("hello", ())

    assert env["type"] == "user"
    assert env["parent_tool_use_id"] is None
    assert env["session_id"] == "default"
    assert env["message"] == {
        "role": "user",
        "content": [{"type": "text", "text": "hello"}],
    }


def test_build_user_message_image_and_pdf_blocks() -> None:
    img = Attachment(media_type="image/png", data=b"\x89PNG", filename=None)
    pdf = Attachment(media_type="application/pdf", data=b"%PDF-1.7", filename="r.pdf")

    blocks = _build_user_message("see attached", (img, pdf))["message"]["content"]

    assert blocks[0] == {"type": "text", "text": "see attached"}
    assert blocks[1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(b"\x89PNG").decode("ascii"),
        },
    }
    # PDFs use a ``document`` block (not ``image``); everything else is an image.
    assert blocks[2]["type"] == "document"
    assert blocks[2]["source"]["media_type"] == "application/pdf"
    assert blocks[2]["source"]["data"] == base64.b64encode(b"%PDF-1.7").decode("ascii")


class CapturingClient:
    """Captures the ``query`` prompt (string or async-iterable) for inspection."""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.connected = False
        self.prompt: Any = None
        self.messages: list[Any] = [
            AssistantMessage(
                content=[TextBlock(text="ok")], model="m", session_id="s1"
            )
        ]

    async def connect(self) -> None:
        self.connected = True

    async def query(
        self, prompt: str | AsyncIterable[dict[str, Any]], session_id: str = "default"
    ) -> None:
        self.prompt = prompt

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self.messages:
            yield message

    async def interrupt(self) -> None: ...

    async def set_model(self, model: str | None = None) -> None: ...

    async def disconnect(self) -> None:
        self.connected = False


def _session(client: CapturingClient) -> TaskSession:
    return TaskSession(model="claude-sonnet-4-6", client_factory=lambda _o: client)


async def test_run_turn_text_only_passes_plain_string() -> None:
    client = CapturingClient(ClaudeAgentOptions())
    session = _session(client)

    events = [event async for event in session.run_turn("hi")]

    assert events[-1] == Final(text="ok")
    assert client.prompt == "hi"  # the SDK's plain-string fast path, no media envelope


async def test_run_turn_with_media_streams_envelope() -> None:
    client = CapturingClient(ClaudeAgentOptions())
    session = _session(client)
    att = Attachment(media_type="image/jpeg", data=b"\xff\xd8\xff", filename=None)

    [event async for event in session.run_turn("what is this?", (att,))]

    assert isinstance(client.prompt, AsyncIterable)
    msgs = [msg async for msg in client.prompt]
    assert len(msgs) == 1  # one-item stream
    content = msgs[0]["message"]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["data"] == base64.b64encode(b"\xff\xd8\xff").decode(
        "ascii"
    )
