"""Per-model context-window resolution for compaction.

The compaction threshold tracks the thread's *current* model window. A model's
window is resolved in three tiers, cheapest first:

1. a ``compaction.windows`` config override (``model-name -> tokens``) — always
   wins, and is the only source for models on a non-OpenRouter backend;
2. OpenRouter's ``/models`` metadata (``context_length``), fetched **once** per
   process and cached as a small ``{id: tokens}`` table — only for models that
   route to the default (OpenRouter) backend, never a named proxy backend;
3. a coded ``default_window`` fallback, used for anything unknown or when the
   fetch is unavailable.

The fetch is best-effort and happens **at most once** per process: a failure
caches an empty table so every later turn falls straight through to
``default_window`` rather than re-hitting the network — ``resolve()`` runs on
every non-forced turn under the session lock, so a persistent ``/models`` outage
must not tax each turn with a wasted (up to ~40s) round-trip. The raw ``/models``
payload (1-2 MB) is discarded — only the ``{id: context_length}`` projection
(tens of KB) is kept.
"""

import asyncio
import logging
from collections.abc import Iterable

import httpx

logger = logging.getLogger(__name__)


class WindowResolver:
    """Resolves a model name to its context window in tokens."""

    def __init__(
        self,
        *,
        windows: dict[str, int],
        default_window: int,
        aliases: Iterable[str],
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._windows = dict(windows)
        self._default = default_window
        self._aliases = set(aliases)
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client
        self._table: dict[str, int] | None = None
        self._lock = asyncio.Lock()

    async def resolve(self, model: str) -> int:
        """Return ``model``'s context window (tokens), falling back to default."""
        if model in self._windows:
            return self._windows[model]
        # An alias routes to a named backend (a local proxy), whose window can't
        # be fetched in OpenRouter's shape — it must come from the config map.
        if model in self._aliases:
            return self._default
        table = await self._table_once()
        return table.get(model, self._default)

    async def _table_once(self) -> dict[str, int]:
        if self._table is not None:
            return self._table
        async with self._lock:
            if self._table is not None:
                return self._table
            # Cache the result either way — an empty table on failure so a
            # persistent /models outage falls back to default_window until
            # restart instead of re-fetching (under the session lock) every turn.
            self._table = await self._fetch() or {}
            return self._table

    async def _fetch(self) -> dict[str, int] | None:
        """Fetch and project OpenRouter ``/models`` to ``{id: context_length}``."""
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10)
        )
        try:
            resp = await client.get(
                f"{self._base_url}/models", headers=self._headers
            )
            if resp.status_code != 200:
                logger.warning("model-window fetch: HTTP %s", resp.status_code)
                return None
            data = resp.json().get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("model-window fetch failed: %s", exc)
            return None
        finally:
            if self._client is None:
                await client.aclose()
        table: dict[str, int] = {}
        for entry in data:
            model_id = entry.get("id")
            context = entry.get("context_length")
            if isinstance(model_id, str) and isinstance(context, int) and context > 0:
                table[model_id] = context
        return table
