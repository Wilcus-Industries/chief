"""Pure view helpers: turn stored wire messages into display rows."""

from typing import Any


def _content_text(content: Any) -> str:
    """The human-readable text of a wire message's content.

    Content is either a plain string or a list of typed blocks (text + tool
    use/result); only the text blocks are shown, the rest is machinery.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def render_transcript(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Map stored wire messages to ``{role, text}`` rows the UI renders.

    Roles are relabeled owner/chief; system prompts and empty turns drop out.
    """
    rows: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _content_text(message.get("content")).strip()
        if not text:
            continue
        rows.append(
            {"role": "owner" if role == "user" else "chief", "text": text}
        )
    return rows
