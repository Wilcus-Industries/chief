"""chief's self-config routing tool (#83, part of #72).

Exercises the real central mechanism: a gated tool call mutating the *persisted*
routing table AND the category set through the shared :class:`RoutingStore`, surviving a
restart (a fresh store loads the edit) and driving the next classify/route. The tools
are invoked through the #80 Copilot adapter
(:func:`chief.core.copilot_tools.sdk_server_to_tools`), so they run under the real
Copilot tool shape — not a stand-in — and reach both backends.

The gating is proved against the real gate (:func:`chief.gate.gate.classify` +
:class:`chief.gate.blacklist.Blacklist` seeded from ``config._DEFAULT_BLACKLIST_TOOLS``)
under the owner default-allow posture: each mutating verb ASKs (a card), while the
read-only ``list_routing`` ALLOWs — i.e. the tool asks rather than silently allowing.
"""

import inspect
from typing import Any

import pytest
from copilot import Tool, ToolInvocation, ToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.config import _DEFAULT_BLACKLIST_TOOLS
from chief.core.copilot_tools import sdk_server_to_tools
from chief.core.routing import RoutingStore, RoutingTarget
from chief.gate.blacklist import Blacklist
from chief.gate.gate import GateDecision, classify
from chief.gate.policy import PolicyStore
from chief.tools.routing_admin import (
    ADD_CATEGORY_TOOL,
    DESCRIBE_CATEGORY_TOOL,
    LIST_ROUTING_TOOL,
    MUTATING_TOOL_NAMES,
    REMOVE_CATEGORY_TOOL,
    RENAME_CATEGORY_TOOL,
    SET_TARGET_TOOL,
    RoutingAdminService,
)

_SEED = [
    ("writing", "copilot", "auto"),
    ("general", "copilot", "auto"),
    ("code", "openrouter", "deepseek/deepseek-v4-flash"),
]


async def _seeded_store(sf: async_sessionmaker[AsyncSession]) -> RoutingStore:
    store = RoutingStore(sf)
    await store.seed(_SEED)
    return store


async def _invoke(tool: Tool, **arguments: Any) -> ToolResult:
    assert tool.handler is not None
    result = tool.handler(ToolInvocation(arguments=arguments))
    return await result if inspect.isawaitable(result) else result


async def _tools(store: RoutingStore) -> dict[str, Tool]:
    """The routing-admin tools as Copilot tools (the #80 adapter — both backends)."""
    service = RoutingAdminService(routing=store)
    tools = await sdk_server_to_tools(service.server_config())
    return {t.name: t for t in tools}


# ---- central mechanism: edits mutate + persist + drive the next resolve -----


async def test_set_target_edit_persists_and_survives_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # AC1: chief changes a category's model via the tool; persisted + used next turn.
    store = await _seeded_store(session_factory)
    tools = await _tools(store)

    result = await _invoke(
        tools[SET_TARGET_TOOL],
        category="writing",
        target_class="openrouter",
        model="anthropic/claude-3.5",
    )
    assert result.result_type == "success"

    # Live cache resolves to the new target (the next spawn reads this synchronously).
    assert store.resolve("writing") == RoutingTarget(
        "writing", "openrouter", "anthropic/claude-3.5"
    )
    # Restart: a fresh store on the same db loads the edit from the routes table.
    reloaded = RoutingStore(session_factory)
    await reloaded.load()
    assert reloaded.resolve("writing") == RoutingTarget(
        "writing", "openrouter", "anthropic/claude-3.5"
    )


async def test_category_set_edits_change_the_classifier_label_space(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # AC2: add / rename / remove a category via the tool; the live category set (the
    # classifier's label space, read at spawn) reflects each edit, and it persists.
    store = await _seeded_store(session_factory)
    tools = await _tools(store)

    await _invoke(
        tools[ADD_CATEGORY_TOOL],
        category="legal",
        target_class="openrouter",
        model="x/y",
        description="contracts and policy",
    )
    await _invoke(tools[RENAME_CATEGORY_TOOL], old="code", new="engineering")
    await _invoke(tools[REMOVE_CATEGORY_TOOL], category="writing")

    assert set(store.categories()) == {"general", "engineering", "legal"}
    assert store.descriptions()["legal"] == "contracts and policy"

    reloaded = RoutingStore(session_factory)
    await reloaded.load()
    assert set(reloaded.categories()) == {"general", "engineering", "legal"}


async def test_describe_category_edit_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded_store(session_factory)
    tools = await _tools(store)

    await _invoke(
        tools[DESCRIBE_CATEGORY_TOOL],
        category="code",
        description="writing or fixing source code",
    )

    reloaded = RoutingStore(session_factory)
    await reloaded.load()
    assert reloaded.descriptions()["code"] == "writing or fixing source code"


async def test_list_routing_reads_the_table(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded_store(session_factory)
    tools = await _tools(store)

    result = await _invoke(tools[LIST_ROUTING_TOOL])

    assert result.result_type == "success"
    text = result.text_result_for_llm
    assert "code: openrouter:deepseek/deepseek-v4-flash" in text
    assert "writing: copilot:auto" in text


# ---- guardrails: the edit can never elevate a class or touch a guest --------


async def test_edit_refuses_a_class_outside_the_allowed_set(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The tool may only ever set copilot/openrouter. A fabricated class (a guest-
    # elevating or a paid one added later) is refused and the table is left unchanged,
    # so the guest-isolation / budget guardrails (which key off the class) stay covered.
    store = await _seeded_store(session_factory)
    tools = await _tools(store)

    result = await _invoke(
        tools[SET_TARGET_TOOL],
        category="writing",
        target_class="future-paid",
        model="x/y",
    )
    assert result.result_type == "failure"
    assert store.resolve("writing") == RoutingTarget("writing", "copilot", "auto")


async def test_edit_of_unknown_category_is_a_clean_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = await _seeded_store(session_factory)
    tools = await _tools(store)

    result = await _invoke(
        tools[SET_TARGET_TOOL],
        category="nope",
        target_class="copilot",
        model="auto",
    )
    assert result.result_type == "failure"
    assert "unknown category" in result.text_result_for_llm


# ---- gating: proved to ASK, not silently ALLOW ------------------------------


def test_mutating_verbs_are_seeded_into_the_default_blacklist() -> None:
    # Under owner default-allow a merely-registered tool ALLOWs with no card; the
    # blacklist seed is what makes each mutating verb ASK.
    for name in MUTATING_TOOL_NAMES:
        assert name in _DEFAULT_BLACKLIST_TOOLS
    # The read-only reader is deliberately NOT blacklisted — it ALLOWs freely.
    assert LIST_ROUTING_TOOL not in _DEFAULT_BLACKLIST_TOOLS


async def test_gate_asks_for_a_mutating_edit_and_allows_the_reader(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The real gate, seeded from the default blacklist: a mutating edit ASKs (a card),
    # the reader ALLOWs — i.e. the tool asks rather than silently allowing.
    policy = PolicyStore(session_factory)
    await policy.seed(never=[], approved=[])
    blacklist = Blacklist.from_config(tools=_DEFAULT_BLACKLIST_TOOLS)

    ask = classify(
        SET_TARGET_TOOL, {}, policy, tier="owner", blacklist=blacklist
    )
    assert ask.decision is GateDecision.ASK

    allow = classify(
        LIST_ROUTING_TOOL, {}, policy, tier="owner", blacklist=blacklist
    )
    assert allow.decision is GateDecision.ALLOW


@pytest.mark.parametrize("name", MUTATING_TOOL_NAMES)
async def test_every_mutating_verb_asks_under_the_gate(
    session_factory: async_sessionmaker[AsyncSession], name: str
) -> None:
    policy = PolicyStore(session_factory)
    await policy.seed(never=[], approved=[])
    blacklist = Blacklist.from_config(tools=_DEFAULT_BLACKLIST_TOOLS)
    verdict = classify(name, {}, policy, tier="owner", blacklist=blacklist)
    assert verdict.decision is GateDecision.ASK
