"""Ambient-recall relevance gate: one classifier call per candidate, then a
pointer nudge — never the note body.

Two deliberate shapes here:

- **Gate, not selector.** Each candidate gets its own yes/no ``classify`` call
  through the core classifier primitive, run concurrently. One label per call
  is exactly what that primitive does, so this drops a hand-rolled "answer with
  numbers" parse whose malformed output silently injected *nothing* — a failure
  that looked identical to "no notes were relevant". It also moves the prompt
  into an owner-editable markdown file in the classifiers dir.
- **Pointer, not payload.** Survivors render as ``path :: heading`` only; the
  agent opens a note with its own file tools if it wants it. Injecting bodies
  spent context on every recall whether the agent used them or not. A nudge
  costs a line, so the gate now protects the *signal* — nudges the agent keeps
  trusting — rather than a token budget.

The candidates are already ranked by vector similarity upstream, so this does
no re-ranking: the gate only removes, and search order survives.
"""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from chief.classifiers import Classifier, ClassifierError

if TYPE_CHECKING:
    from chief_obsidian_memory.index import SearchHit

# Seeded into the classifiers dir by the package install; editable by the owner.
CLASSIFIER_NAME = "memory-relevance"
RELEVANT = "RELEVANT"

_NUDGE_HEADER = (
    "Possibly relevant notes in the owner's Obsidian vault. These are pointers, "
    "not content — each line is an absolute path; open one with your file tools "
    "(read_file) only if it would help:"
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
    classifier: Classifier,
    transcript: str,
    candidates: "list[SearchHit]",
    cap_tokens: int,
    vault: Path,
) -> str | None:
    """Gate each candidate concurrently; return the pointer nudge, or ``None``
    when nothing survives. ``vault`` is the vault root the pointers resolve
    against, so each nudge names an absolute, directly-openable path."""
    if not candidates:
        return None
    verdicts = await asyncio.gather(
        *(_relevant(classifier, transcript, hit) for hit in candidates)
    )
    kept = [hit for hit, keep in zip(candidates, verdicts, strict=True) if keep]
    if not kept:
        return None
    return _truncate(_format(kept, vault), cap_tokens)


async def _relevant(
    classifier: Classifier, transcript: str, hit: "SearchHit"
) -> bool:
    """One candidate's verdict. A classifier that never resolves a label drops
    the candidate rather than failing the turn — a missed nudge is recoverable
    (the agent can still search), a raised recall hook is not."""
    try:
        label = await classifier.classify(CLASSIFIER_NAME, _payload(transcript, hit))
    except ClassifierError:
        return False
    return label == RELEVANT


def _payload(transcript: str, hit: "SearchHit") -> str:
    """Transcript first, candidate last: every candidate in a run shares the
    transcript prefix, so the provider can cache it across the concurrent calls.
    """
    return (
        f"{transcript}\n\nCandidate note:\n"
        f"{hit.note_path} :: {hit.heading}\n{hit.text}"
    )


def _format(candidates: "list[SearchHit]", vault: Path) -> str:
    # Absolute path, not the vault-relative one: the agent opens a nudge with its
    # file tools, and a bare `contacts/family.md` does not resolve from the repo
    # root it runs in — it would (and did) flail finding the note.
    body = "\n".join(
        f"- {vault / c.note_path} :: {c.heading}" for c in candidates
    )
    return f"{_NUDGE_HEADER}\n{body}"


def _truncate(text: str, cap_tokens: int) -> str:
    # Pointers are short by construction; this is a backstop against a vault
    # with pathological paths or headings, not the primary budget control.
    # ~4 characters per token is a good-enough budget without a tokenizer.
    limit = max(0, cap_tokens) * 4
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _text_of(content: object) -> str:
    return content if isinstance(content, str) else str(content)
