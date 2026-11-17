"""The messaging loop guard: a warning, never a refusal (PRD #308).

Counted per unordered session pair rather than per ``reply_to`` chain, because
models often leave ``reply_to`` out and a chain count would never trip. The
budget cap is the hard backstop; this only nudges two sessions that keep
talking to stop if they are looping.
"""

import time
from collections import deque
from collections.abc import Callable

LOOP_WARNING = (
    "WARNING: You've been sending a lot of messages. If this is unnecessary "
    "or you are looping, stop."
)


class PairCounter:
    """Rolling-window message count per unordered pair of senders."""

    def __init__(
        self,
        threshold: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._window = window_s
        self._clock = clock
        self._seen: dict[frozenset[str], deque[float]] = {}

    def note(self, a: str, b: str) -> bool:
        """Count one message between ``a`` and ``b``; True once the count in
        the window has reached the threshold (both sides should be warned)."""
        now = self._clock()
        stamps = self._seen.setdefault(frozenset({a, b}), deque())
        stamps.append(now)
        while stamps and stamps[0] <= now - self._window:
            stamps.popleft()
        return len(stamps) >= self._threshold
