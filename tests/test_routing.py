"""RoutingStore + provider materialisation (issue #79, part of #72).

The real route→resolve central logic: the config seed lands in the ``routes`` table, the
store caches it, ``resolve`` maps a category to its ``{target_class, model}`` target,
and ``provider_for_target`` materialises the BYOK provider (openrouter) vs plain Copilot
quota (copilot). No mock of the routing itself — only sqlite is the in-memory fixture.
"""

import pytest
from copilot import ProviderConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.core.routing import (
    TARGET_CLASS_COPILOT,
    TARGET_CLASS_OPENROUTER,
    RoutingEditError,
    RoutingStore,
    RoutingTarget,
    provider_for_target,
)
from chief.persistence.routing import add_route, list_routes

_SEED = [
    ("writing", "copilot", "auto"),
    ("research", "copilot", "auto"),
    ("general", "copilot", "auto"),
    ("code", "openrouter", "deepseek/deepseek-v4-flash"),
    ("reasoning", "openrouter", "deepseek/deepseek-v4-flash"),
]


async def _seeded(sf: async_sessionmaker[AsyncSession]) -> RoutingStore:
    store = RoutingStore(sf)
    await store.seed(_SEED)
    return store


async def test_seed_then_resolve_maps_category_to_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded(session_factory)

    assert store.resolve("writing") == RoutingTarget("writing", "copilot", "auto")
    assert store.resolve("code") == RoutingTarget(
        "code", "openrouter", "deepseek/deepseek-v4-flash"
    )
    reasoning = store.resolve("reasoning")
    assert reasoning is not None and reasoning.target_class == TARGET_CLASS_OPENROUTER


async def test_categories_is_the_persisted_row_set(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded(session_factory)
    # The category set is persisted *as data* — exactly the seeded rows, no more.
    assert set(store.categories()) == {
        "writing",
        "research",
        "general",
        "code",
        "reasoning",
    }
    assert store.has("code") is True
    assert store.has("nope") is False


async def test_seed_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = RoutingStore(session_factory)
    await store.seed(_SEED)
    await store.seed(_SEED)  # a re-seed (restart) must not duplicate rows
    async with session_factory() as session:
        rows = await list_routes(session)
    assert len(rows) == len(_SEED)


async def test_resolve_unknown_falls_back_to_default_category(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded(session_factory)
    # An unknown category resolves to the default ("general" → copilot auto), never None
    # once seeded, so the engine always has a target to spawn on.
    assert store.resolve("mystery") == RoutingTarget("general", "copilot", "auto")


async def test_resolve_none_when_table_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = RoutingStore(session_factory)
    await store.load()
    assert store.resolve("code") is None  # unseeded → no target


async def test_load_reflects_a_direct_row_add(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = RoutingStore(session_factory)
    async with session_factory() as session:
        await add_route(
            session, category="code", target_class="openrouter", model="x/y"
        )
    await store.load()
    assert store.resolve("code") == RoutingTarget("code", "openrouter", "x/y")


def test_provider_for_openrouter_target_is_the_byok_provider() -> None:
    provider = ProviderConfig(base_url="https://openrouter.ai/api/v1", api_key="k")
    target = RoutingTarget(
        "code", TARGET_CLASS_OPENROUTER, "deepseek/deepseek-v4-flash"
    )
    assert provider_for_target(target, openrouter_provider=provider) is provider


def test_provider_for_copilot_target_is_none() -> None:
    # A copilot-quota target stays on plain Copilot quota — no BYOK provider — even if
    # an openrouter provider is available.
    provider = ProviderConfig(base_url="https://openrouter.ai/api/v1", api_key="k")
    target = RoutingTarget("writing", TARGET_CLASS_COPILOT, "auto")
    assert provider_for_target(target, openrouter_provider=provider) is None


# ---- self-config edits (#83) ------------------------------------------------


async def test_set_target_repoints_category_and_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded(session_factory)

    await store.set_target("writing", target_class="openrouter", model="x/y")

    # The live cache resolves to the new target immediately (drives the next spawn)...
    assert store.resolve("writing") == RoutingTarget("writing", "openrouter", "x/y")
    # ...and a fresh store on the same db loads it (survives a restart).
    reloaded = RoutingStore(session_factory)
    await reloaded.load()
    assert reloaded.resolve("writing") == RoutingTarget("writing", "openrouter", "x/y")


async def test_set_target_rejects_unknown_category(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded(session_factory)
    with pytest.raises(RoutingEditError, match="unknown category"):
        await store.set_target("nope", target_class="copilot", model="auto")


async def test_edits_reject_a_class_outside_the_allowed_set(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Guardrail: an edit may only ever set copilot/openrouter — never a fabricated class
    # (a guest-elevating or paid one) the downstream guardrails don't cover.
    store = await _seeded(session_factory)
    with pytest.raises(RoutingEditError, match="target_class must be one of"):
        await store.set_target("writing", target_class="future-paid", model="x/y")
    with pytest.raises(RoutingEditError, match="target_class must be one of"):
        await store.add_category("evil", target_class="guest", model="x/y")


async def test_add_category_grows_the_label_space_and_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded(session_factory)

    await store.add_category(
        "legal", target_class="openrouter", model="x/y", description="contracts"
    )

    assert "legal" in store.categories()
    assert store.descriptions()["legal"] == "contracts"
    reloaded = RoutingStore(session_factory)
    await reloaded.load()
    assert reloaded.resolve("legal") == RoutingTarget("legal", "openrouter", "x/y")
    assert reloaded.descriptions()["legal"] == "contracts"


async def test_add_category_rejects_duplicate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded(session_factory)
    with pytest.raises(RoutingEditError, match="already exists"):
        await store.add_category("code", target_class="copilot", model="auto")


async def test_rename_category_moves_the_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded(session_factory)

    await store.rename_category("code", "engineering")

    assert "code" not in store.categories()
    assert "engineering" in store.categories()
    assert store.resolve("engineering") == RoutingTarget(
        "engineering", "openrouter", "deepseek/deepseek-v4-flash"
    )
    reloaded = RoutingStore(session_factory)
    await reloaded.load()
    assert "engineering" in reloaded.categories()


async def test_rename_rejects_existing_target_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded(session_factory)
    with pytest.raises(RoutingEditError, match="already exists"):
        await store.rename_category("code", "writing")


async def test_remove_category_drops_the_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded(session_factory)

    await store.remove_category("code")

    assert "code" not in store.categories()
    reloaded = RoutingStore(session_factory)
    await reloaded.load()
    assert "code" not in reloaded.categories()


async def test_default_category_cannot_be_removed_or_renamed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The resolve fallback keys off the default category, so an edit can't orphan it.
    store = await _seeded(session_factory)
    with pytest.raises(RoutingEditError, match="default category"):
        await store.remove_category("general")
    with pytest.raises(RoutingEditError, match="default category"):
        await store.rename_category("general", "misc")


async def test_set_description_updates_and_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded(session_factory)

    await store.set_description("code", "writing or fixing source code")

    assert store.descriptions()["code"] == "writing or fixing source code"
    reloaded = RoutingStore(session_factory)
    await reloaded.load()
    assert reloaded.descriptions()["code"] == "writing or fixing source code"
    # Clearing it drops it from the descriptions map.
    await store.set_description("code", None)
    assert "code" not in store.descriptions()
