"""Per-thread stream policy: what a turn streams to a tapped-in observer.

A turn always emits its coarse ``tick`` and (for non-web threads) the rich
``inbound``/``final``; this policy gates only the *extra* live detail — token
``delta``s, per-call ``tool`` ticks, and whether a tool's ``result`` loads
live (``lazy`` = only on click via /history/tool, ``inline`` = streamed as a
``result`` frame, ``off`` = no live result at all, and the ``tool`` tick omits
its ``call_id`` so no load button binds to it). History always renders full,
independent of this — the policy governs the live stream only.

Resolution order: a session row's stored override wins; else the channel's
configured default; else :data:`COARSE`. ``send_guard`` is carried and
persisted here but not enforced this slice (a later reply-from-web slice reads
it).
"""

from dataclasses import dataclass, fields

RESULT_MODES = ("lazy", "inline", "off")


@dataclass(frozen=True)
class StreamPolicy:
    """What extra live detail a thread's turns stream to tapped-in observers."""

    deltas: bool = False
    tools: bool = False
    results: str = "off"
    send_guard: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "deltas": self.deltas,
            "tools": self.tools,
            "results": self.results,
            "send_guard": self.send_guard,
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, object], base: "StreamPolicy | None" = None
    ) -> "StreamPolicy":
        """Build a policy, filling each missing key from ``base`` (or the field
        default). An unknown ``results`` mode is refused — a typo that silently
        read as ``off`` would quietly kill live result loading."""
        base = base or cls()
        merged = {f.name: getattr(base, f.name) for f in fields(cls)}
        merged.update({k: v for k, v in data.items() if k in merged})
        results = merged["results"]
        if results not in RESULT_MODES:
            raise ValueError(
                f"stream policy results must be one of {RESULT_MODES}, "
                f"got {results!r}"
            )
        return cls(
            deltas=bool(merged["deltas"]),
            tools=bool(merged["tools"]),
            results=str(results),
            send_guard=bool(merged["send_guard"]),
        )


RICH = StreamPolicy(deltas=True, tools=True, results="lazy", send_guard=True)
COARSE = StreamPolicy()

# Seeded channel defaults. imessage and web are the observer surfaces (the web
# cockpit is *the* live view, and already emits web-origin tool ticks through
# dispatch), so both stream rich; cli/system (monitor/cron) wakes are absent →
# they resolve to COARSE.
DEFAULT_CHANNEL_DEFAULTS: dict[str, StreamPolicy] = {"imessage": RICH, "web": RICH}


def resolve(
    channel: str,
    override: dict[str, object] | None,
    channel_defaults: dict[str, StreamPolicy],
) -> StreamPolicy:
    """A thread's effective policy: a stored row override wins, else the
    channel default, else :data:`COARSE`."""
    if override is not None:
        return StreamPolicy.from_dict(override)
    return channel_defaults.get(channel, COARSE)


# Frame builders — a policy decides *which* live frames a turn emits. Each
# returns the observer-hub frame dict, or None when the policy suppresses it.


def delta_frame(
    policy: StreamPolicy, thread: str, text: str
) -> dict[str, object] | None:
    if not policy.deltas:
        return None
    return {"type": "delta", "thread": thread, "text": text}


def tool_frame(
    policy: StreamPolicy, thread: str, *, name: str, call_id: str
) -> dict[str, object] | None:
    if not policy.tools:
        return None
    frame: dict[str, object] = {"type": "tool", "thread": thread, "name": name}
    # call_id is the handle the live view loads a result by; under results=off
    # we withhold it so nothing loads live (history keeps it — AC5).
    if policy.results != "off":
        frame["call_id"] = call_id
    return frame


def result_frame(
    policy: StreamPolicy, thread: str, *, call_id: str, result: str
) -> dict[str, object] | None:
    if not (policy.tools and policy.results == "inline"):
        return None
    return {"type": "result", "thread": thread, "call_id": call_id,
            "result": result}
