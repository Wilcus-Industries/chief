"""Approval broker: first answer wins, fail-closed timeout, one card a time."""

import asyncio

from chief.approvals import ApprovalBroker

SENT: list[str] = []


async def send(text: str) -> None:
    SENT.append(text)


async def test_yes_answer_approves() -> None:
    broker = ApprovalBroker()
    task = asyncio.create_task(broker.ask("cli:t", "ok?", send))
    await asyncio.sleep(0.01)
    assert broker.resolve("cli:t", " YES ") is True
    assert await task is True


async def test_any_non_yes_answer_denies() -> None:
    broker = ApprovalBroker()
    task = asyncio.create_task(broker.ask("cli:t", "ok?", send))
    await asyncio.sleep(0.01)
    assert broker.resolve("cli:t", "hmm let me think") is True
    assert await task is False


async def test_timeout_fails_closed() -> None:
    broker = ApprovalBroker(timeout=0.02)
    assert await broker.ask("cli:t", "ok?", send) is False


async def test_unrelated_thread_is_not_consumed() -> None:
    broker = ApprovalBroker(timeout=0.05)
    task = asyncio.create_task(broker.ask("cli:t", "ok?", send))
    await asyncio.sleep(0.01)
    assert broker.resolve("cli:other", "yes") is False
    assert broker.resolve("cli:t", "yes") is True
    assert await task is True


async def test_second_card_on_same_thread_denies_immediately() -> None:
    broker = ApprovalBroker(timeout=0.5)
    task = asyncio.create_task(broker.ask("cli:t", "first?", send))
    await asyncio.sleep(0.01)
    assert await broker.ask("cli:t", "second?", send) is False
    broker.resolve("cli:t", "yes")
    assert await task is True
