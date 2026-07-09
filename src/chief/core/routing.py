"""Job categories → routing targets (issue #79, part of #72).

A task's spawning message is auto-classified into one job *category* (writing / code /
reasoning / research / general by default); the routing table maps each category to a
``{target_class, model}`` target. Two target classes exist at this slice:

* ``copilot`` — plain Copilot quota (the Student plan's ``auto``; no per-model choice —
  spike #74 — so ``set_model`` is untrusted and the served model is read from the event
  instead). The BYOK provider is ``None``: the session stays on Copilot quota.
* ``openrouter`` — BYOK, per-model (the provider slice #90): the SDK's "openai" provider
  pointed at OpenRouter, with the row's ``model`` requested explicitly via ``model=``.

The category set is persisted *as data* — it is exactly the set of categories with a row
in the ``routes`` table (:class:`~chief.persistence.models.Route`), seeded from config
on boot (mirrors :class:`~chief.gate.policy.PolicyStore`). :class:`RoutingStore` caches
the table in memory so :meth:`RoutingStore.resolve` is a sync hot-path read at task
spawn; :func:`provider_for_target` materialises a target's BYOK provider. The classifier
that picks the category (:func:`chief.core.classify.classify_category`) runs on a fixed
cheap model and is **never** routed through this table.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from copilot import ProviderConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..persistence.routing import add_route, list_routes

#: The two target classes at this slice (spike #74 / provider slice #90).
TARGET_CLASS_COPILOT = "copilot"
TARGET_CLASS_OPENROUTER = "openrouter"
TARGET_CLASSES: tuple[str, ...] = (TARGET_CLASS_COPILOT, TARGET_CLASS_OPENROUTER)

#: The initial job-category set and the safe fallback the classifier fails to.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "writing",
    "code",
    "reasoning",
    "research",
    "general",
)
DEFAULT_CATEGORY = "general"


@dataclass(frozen=True)
class RoutingTarget:
    """A resolved routing row: a category + the ``{target_class, model}`` it maps to."""

    category: str
    target_class: str
    model: str


def provider_for_target(
    target: RoutingTarget, *, openrouter_provider: ProviderConfig | None
) -> ProviderConfig | None:
    """The BYOK provider a target's session opens on.

    ``openrouter`` targets carry ``openrouter_provider`` (built once from settings by
    :func:`chief.core.copilot_session.openrouter_provider_config`); ``copilot`` targets
    carry ``None`` — the session stays on plain Copilot quota.
    """
    if target.target_class == TARGET_CLASS_OPENROUTER:
        return openrouter_provider
    return None


class RoutingStore:
    """In-memory category→target table, backed by the ``routes`` table.

    Loaded once on boot and kept in memory so :meth:`resolve` is a synchronous hot-path
    call at task spawn; :meth:`seed` writes the config seed through to the table
    (mirrors :class:`~chief.gate.policy.PolicyStore`). The self-config slice mutates it.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        default_category: str = DEFAULT_CATEGORY,
    ) -> None:
        self._sf = session_factory
        self._default_category = default_category
        self._targets: dict[str, RoutingTarget] = {}

    async def load(self) -> None:
        """Replace the in-memory cache from the table."""
        async with self._sf() as session:
            rows = await list_routes(session)
        self._targets = {
            row.category: RoutingTarget(row.category, row.target_class, row.model)
            for row in rows
        }

    async def seed(self, targets: Iterable[tuple[str, str, str]]) -> None:
        """Idempotently insert ``(category, target_class, model)`` rows, then reload."""
        async with self._sf() as session:
            for category, target_class, model in targets:
                await add_route(
                    session,
                    category=category,
                    target_class=target_class,
                    model=model,
                )
        await self.load()

    def categories(self) -> tuple[str, ...]:
        """The persisted category set — the classifier's label space."""
        return tuple(self._targets)

    def has(self, category: str) -> bool:
        """Whether ``category`` has its own row (``/route`` validity check)."""
        return category in self._targets

    def resolve(self, category: str) -> RoutingTarget | None:
        """The target for ``category``, falling back to the default category's target.

        ``None`` only when neither ``category`` nor the default category is configured
        (an unseeded table) — so a caller can treat ``None`` as "routing has no target".
        """
        target = self._targets.get(category)
        if target is not None:
            return target
        return self._targets.get(self._default_category)
