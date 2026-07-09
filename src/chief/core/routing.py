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

from ..persistence.routing import (
    add_route,
    list_routes,
    remove_route,
    rename_route,
    set_route_description,
    set_route_target,
)

#: The two target classes at this slice (spike #74 / provider slice #90).
TARGET_CLASS_COPILOT = "copilot"
TARGET_CLASS_OPENROUTER = "openrouter"
TARGET_CLASSES: tuple[str, ...] = (TARGET_CLASS_COPILOT, TARGET_CLASS_OPENROUTER)


class RoutingEditError(ValueError):
    """A self-config routing edit the store refused (bad class, unknown/duplicate name).

    The security-relevant one is an unknown ``target_class``: the edit tool may only
    ever set one of :data:`TARGET_CLASSES`, so it can never invent a class the
    guest-isolation / budget guardrails (which key off the class) don't already cover.
    """

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
        self._descriptions: dict[str, str] = {}

    async def load(self) -> None:
        """Replace the in-memory cache from the table."""
        async with self._sf() as session:
            rows = await list_routes(session)
        self._targets = {
            row.category: RoutingTarget(row.category, row.target_class, row.model)
            for row in rows
        }
        self._descriptions = {
            row.category: row.description for row in rows if row.description
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

    def descriptions(self) -> dict[str, str]:
        """Category → description for each described category (classifier seam)."""
        return dict(self._descriptions)

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

    # ---- self-config edits (#83) ----------------------------------------
    #
    # Each edit validates against the in-memory cache, writes through to the ``routes``
    # table, then reloads — so the next classify/route (a sync read of the cache) sees
    # it, and it survives a restart (the table is reseeded-then-loaded on boot). The
    # class check is the guardrail: only :data:`TARGET_CLASSES` are accepted, so an edit
    # can never mint a class the guest/budget guardrails don't already key off.

    @staticmethod
    def _check_class(target_class: str) -> None:
        if target_class not in TARGET_CLASSES:
            raise RoutingEditError(
                f"target_class must be one of {TARGET_CLASSES}, got {target_class!r}"
            )

    async def set_target(
        self, category: str, *, target_class: str, model: str
    ) -> None:
        """Repoint an existing ``category`` at ``{target_class, model}``."""
        self._check_class(target_class)
        if not model.strip():
            raise RoutingEditError("model must be non-empty")
        if category not in self._targets:
            raise RoutingEditError(f"unknown category {category!r}")
        async with self._sf() as session:
            await set_route_target(
                session, category=category, target_class=target_class, model=model
            )
        await self.load()

    async def add_category(
        self,
        category: str,
        *,
        target_class: str,
        model: str,
        description: str | None = None,
    ) -> None:
        """Add a new category row (with its target and optional description)."""
        self._check_class(target_class)
        category = category.strip()
        if not category or not model.strip():
            raise RoutingEditError("category and model must be non-empty")
        if category in self._targets:
            raise RoutingEditError(f"category {category!r} already exists")
        async with self._sf() as session:
            await add_route(
                session,
                category=category,
                target_class=target_class,
                model=model,
                description=description,
            )
        await self.load()

    async def remove_category(self, category: str) -> None:
        """Remove a category row. The default fallback category can't be removed."""
        if category not in self._targets:
            raise RoutingEditError(f"unknown category {category!r}")
        if category == self._default_category:
            raise RoutingEditError(
                f"cannot remove the default category {category!r} "
                "(the routing fallback would break)"
            )
        async with self._sf() as session:
            await remove_route(session, category=category)
        await self.load()

    async def rename_category(self, old: str, new: str) -> None:
        """Rename ``old`` to ``new``. The default fallback category can't be renamed."""
        new = new.strip()
        if not new:
            raise RoutingEditError("the new category name must be non-empty")
        if old not in self._targets:
            raise RoutingEditError(f"unknown category {old!r}")
        if old == self._default_category:
            raise RoutingEditError(
                f"cannot rename the default category {old!r} "
                "(the routing fallback would break)"
            )
        if new in self._targets:
            raise RoutingEditError(f"category {new!r} already exists")
        async with self._sf() as session:
            await rename_route(session, old=old, new=new)
        await self.load()

    async def set_description(
        self, category: str, description: str | None
    ) -> None:
        """Set (or clear, with ``None``) an existing ``category``'s description."""
        if category not in self._targets:
            raise RoutingEditError(f"unknown category {category!r}")
        async with self._sf() as session:
            await set_route_description(
                session, category=category, description=description
            )
        await self.load()
