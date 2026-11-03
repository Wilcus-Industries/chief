"""Pre-generation retry shared by the streaming HTTP backends."""

import asyncio
from collections.abc import AsyncIterator, Callable

import httpx

from chief.provider.base import ProviderError, ProviderEvent

Attempt = Callable[[list[bool]], AsyncIterator[ProviderEvent]]


async def stream_with_retry(
    attempt: Attempt, *, max_retries: int, backoff: float, where: str
) -> AsyncIterator[ProviderEvent]:
    """Run ``attempt`` and retry transport failures that struck before any data.

    ``attempt`` flips ``progressed[0]`` on the first real stream chunk. The guard
    is "did any data arrive", not "did we yield text": a tool-call-only turn
    accumulates fragments without yielding, and retrying after those arrived
    would make the model re-generate the whole turn (extra cost against the
    budget cap), not merely reconnect. Once bytes have arrived a drop fails
    loud, naming the underlying exception (a dropped stream is a ``ReadError``,
    not literally "unreachable").
    """
    for attempt_number in range(max_retries + 1):
        progressed = [False]
        try:
            async for event in attempt(progressed):
                yield event
            return
        except httpx.TransportError as exc:
            if progressed[0] or attempt_number == max_retries:
                raise ProviderError(
                    f"backend unreachable ({where}): {type(exc).__name__}: {exc}"
                ) from exc
            await asyncio.sleep(backoff)
