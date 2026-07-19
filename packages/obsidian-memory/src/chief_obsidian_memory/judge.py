"""The ambient-recall relevance judge: one in-process model call, no shell-out.

The conversation is rendered as a single system message — the judge reads it as
data, never mistaking the transcript for its own instructions. Candidate notes
are pre-fetched by the hook from the in-process index (never a subprocess) and
listed alongside; the judge picks the relevant ones in one completion, and the
selection is formatted and truncated to the token cap before injection. A full
tool-driven search loop is a possible follow-up; this ships the cheap path.
"""

import re
from typing import TYPE_CHECKING

from chief.provider.base import Completion, Provider

if TYPE_CHECKING:
    from chief_obsidian_memory.index import SearchHit

_INSTRUCTIONS = (
    "You are a memory relevance judge for the owner's personal Obsidian vault. "
    "Below is the recent conversation, then a numbered list of candidate notes. "
    "Choose only the candidates that would genuinely help the assistant's next "
    "reply. Answer with the numbers of the relevant candidates separated by "
    "commas (for example: 1, 3), or exactly 'none' if none apply."
)


def format_transcript(
    messages: list[dict[str, object]], user_text: str, window: int
) -> str:
    """Render the last ``window`` messages plus the incoming turn as one block."""
    recent = messages[-window:] if window else list(messages)
    lines = ["Conversation so far:"]
    lines += [f"{m.get('role', '?')}: {_text_of(m.get('content'))}" for m in recent]
    lines.append(f"user (incoming): {user_text}")
    return "\n".join(lines)


async def run_judge(
    provider: Provider,
    model: str,
    transcript: str,
    candidates: "list[SearchHit]",
    cap_tokens: int,
) -> str | None:
    """Ask the judge which candidates are relevant; return the formatted, capped
    injection text, or ``None`` when nothing is chosen."""
    if not candidates:
        return None
    reply = await _complete(provider, model, _prompt(transcript, candidates))
    chosen = _parse_selection(reply, len(candidates))
    if not chosen:
        return None
    return _truncate(_format([candidates[i] for i in chosen]), cap_tokens)


def _prompt(transcript: str, candidates: "list[SearchHit]") -> str:
    listing = "\n".join(
        f"[{i + 1}] {c.note_path} :: {c.heading}\n{c.text}"
        for i, c in enumerate(candidates)
    )
    return f"{_INSTRUCTIONS}\n\n{transcript}\n\nCandidate notes:\n{listing}"


async def _complete(provider: Provider, model: str, content: str) -> str:
    messages = [{"role": "system", "content": content}]
    final = ""
    async for event in provider.stream(model=model, messages=messages, tools=[]):
        if isinstance(event, Completion):
            final = event.text
    return final


def _parse_selection(reply: str, count: int) -> list[int]:
    if "none" in reply.lower():
        return []
    picked = {int(n) - 1 for n in re.findall(r"\d+", reply)}
    return sorted(i for i in picked if 0 <= i < count)


def _format(candidates: "list[SearchHit]") -> str:
    body = "\n".join(
        f"- {c.note_path} :: {c.heading}\n  {c.text}" for c in candidates
    )
    return f"Relevant notes from your Obsidian vault:\n{body}"


def _truncate(text: str, cap_tokens: int) -> str:
    # ~4 characters per token is a good-enough budget without a tokenizer.
    limit = max(0, cap_tokens) * 4
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _text_of(content: object) -> str:
    return content if isinstance(content, str) else str(content)
