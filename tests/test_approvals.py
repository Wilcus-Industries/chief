"""Approval broker: first answer wins, fail-closed timeout, one card a time."""

import asyncio

from chief.approvals import Approval, ApprovalBroker, parse_answer

SENT: list[str] = []


async def send(text: str) -> None:
    SENT.append(text)


def test_parse_answer_maps_verdicts() -> None:
    assert parse_answer(" YES ") is Approval.ONCE
    assert parse_answer("always allow") is Approval.ALWAYS
    assert parse_answer("a") is Approval.ALWAYS
    assert parse_answer("hmm let me think") is Approval.DENY


async def test_yes_answer_approves_once() -> None:
    broker = ApprovalBroker()
    task = asyncio.create_task(broker.ask("cli:t", "ok?", send))
    await asyncio.sleep(0.01)
    assert broker.resolve("cli:t", " YES ") is True
    assert await task is Approval.ONCE


async def test_always_answer_is_carried_through() -> None:
    broker = ApprovalBroker()
    task = asyncio.create_task(broker.ask("cli:t", "ok?", send))
    await asyncio.sleep(0.01)
    assert broker.resolve("cli:t", "always") is True
    assert await task is Approval.ALWAYS


async def test_any_non_yes_answer_denies() -> None:
    broker = ApprovalBroker()
    task = asyncio.create_task(broker.ask("cli:t", "ok?", send))
    await asyncio.sleep(0.01)
    assert broker.resolve("cli:t", "hmm let me think") is True
    assert await task is Approval.DENY


async def test_timeout_fails_closed() -> None:
    broker = ApprovalBroker(timeout=0.02)
    assert await broker.ask("cli:t", "ok?", send) is Approval.DENY


async def test_unrelated_thread_is_not_consumed() -> None:
    broker = ApprovalBroker(timeout=0.05)
    task = asyncio.create_task(broker.ask("cli:t", "ok?", send))
    await asyncio.sleep(0.01)
    assert broker.resolve("cli:other", "yes") is False
    assert broker.resolve("cli:t", "yes") is True
    assert await task is Approval.ONCE


async def test_pending_question_lets_a_late_joiner_see_the_card() -> None:
    """A dashboard client tapping in after the card was raised must still be
    able to render it — not stall silently (#267 AC4)."""
    broker = ApprovalBroker()
    assert broker.pending_question("cli:t") is None
    task = asyncio.create_task(broker.ask("cli:t", "ok?", send))
    await asyncio.sleep(0.01)
    assert broker.pending_question("cli:t") == "ok?"
    assert broker.resolve("cli:t", "yes") is True
    assert await task is Approval.ONCE
    assert broker.pending_question("cli:t") is None  # cleared once answered


async def test_second_card_on_same_thread_denies_immediately() -> None:
    broker = ApprovalBroker(timeout=0.5)
    task = asyncio.create_task(broker.ask("cli:t", "first?", send))
    await asyncio.sleep(0.01)
    assert await broker.ask("cli:t", "second?", send) is Approval.DENY
    broker.resolve("cli:t", "yes")
    assert await task is Approval.ONCE
