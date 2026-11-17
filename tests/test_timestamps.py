"""Arrival stamps on transcript messages.

A user or tool message is stored with its raw ``content`` and a ``stamp`` beside
it; the stamp is folded in front of the content only when a model is sent the
transcript (``provider.base.for_model``). The stamp names when the message
*arrived*, formatted once and kept, so the model-facing prefix never changes.

Every test here runs with real stamps: the suite's autouse
``_plain_message_content`` builds messages unstamped for every other module
unless a test carries ``@pytest.mark.timestamps``.
"""

import asyncio
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Message
from chief.agent.loop import run_turn
from chief.agent.subagent_drive import RunDeps
from chief.agent.subagent_pool import SubagentPool
from chief.agent.timestamps import stamp, tool_message, user_message
from chief.audit import AuditLog
from chief.dispatch import Dispatcher
from chief.persistence.store import MessageStore
from chief.provider.base import STAMP_KEY, for_model
from chief.selfedit.inflight import INFLIGHT_FILE, InflightLog
from chief.selfedit.resume import resume_plan
from chief.subagents import AgentDef
from chief.tools import ToolRegistry

from .fakes import FakeProvider, text_turn, tool_turn
from .test_budget import make_budget
from .test_budget import make_manager as make_budget_manager
from .test_dispatch import RecordingAdapter
from .test_inject import Gate, item, manager_for
from .test_loop import echo_registry
from .test_session import make_manager, noop_delta

pytestmark = pytest.mark.timestamps

LABEL = r"\[(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d) \S+, UTC([+-]\d\d:\d\d)\]"
# A message that arrives just before midnight UTC: the case where stamping the
# moment it is *processed* would put it on the wrong day.
ARRIVED = datetime(2026, 11, 12, 23, 58, tzinfo=UTC).timestamp()
ARRIVED_STAMP = "[2026-11-12 23:58:00 UTC, UTC+00:00]"


@pytest.fixture
def zone() -> Iterator[Callable[[str], None]]:
    """Switch the process time zone mid-test. ``MonkeyPatch.context`` restores
    ``TZ`` on exit; ``tzset`` then runs once more so no later test inherits the
    zone (restoring the variable alone does not re-read it)."""
    with pytest.MonkeyPatch.context() as patch:

        def use(name: str) -> None:
            patch.setenv("TZ", name)
            time.tzset()

        yield use
    time.tzset()


def moment_of(label: str) -> datetime:
    """The instant a stamp names — the numeric offset decides, not the zone's
    abbreviation."""
    match = re.fullmatch(LABEL, label)
    assert match, f"not a stamp: {label!r}"
    date, clock, offset = match.groups()
    return datetime.fromisoformat(f"{date}T{clock}{offset}")


def is_recent(label: str) -> bool:
    return abs(moment_of(label) - datetime.now(UTC)) < timedelta(seconds=5)


def split_stamp(content: str) -> tuple[str, str]:
    """A model-facing content string as (its stamp, the text after it)."""
    label, _, text = content.partition("] ")
    return f"{label}]", text


async def until(condition: Callable[[], bool]) -> None:
    while not condition():
        await asyncio.sleep(0)


# ---- the stamp --------------------------------------------------------------


def test_stamp_names_the_local_time_zone_and_utc_offset(
    zone: Callable[[str], None],
) -> None:
    zone("America/New_York")
    at = datetime(2026, 11, 12, 22, 30, 5, tzinfo=UTC).timestamp()

    assert stamp(at) == "[2026-11-12 17:30:05 EST, UTC-05:00]"


def test_stamp_follows_daylight_saving(zone: Callable[[str], None]) -> None:
    zone("America/New_York")
    at = datetime(2026, 7, 1, 16, 0, 0, tzinfo=UTC).timestamp()

    assert stamp(at) == "[2026-07-01 12:00:00 EDT, UTC-04:00]"


def test_stamp_handles_a_positive_half_hour_offset(zone: Callable[[str], None]) -> None:
    zone("Asia/Kolkata")
    at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC).timestamp()

    assert stamp(at) == "[2026-01-01 05:30:00 IST, UTC+05:30]"


def test_stamp_defaults_to_now() -> None:
    assert is_recent(stamp())


def test_a_stamp_in_any_zone_names_the_same_instant(
    zone: Callable[[str], None],
) -> None:
    zone("Asia/Kolkata")
    in_india = stamp(ARRIVED)
    zone("America/New_York")
    in_new_york = stamp(ARRIVED)

    instant = datetime.fromtimestamp(ARRIVED, UTC)
    assert in_india != in_new_york
    assert moment_of(in_india) == moment_of(in_new_york) == instant


# ---- the message builders ---------------------------------------------------


def test_a_user_message_keeps_its_text_raw_and_carries_the_arrival_stamp(
    zone: Callable[[str], None],
) -> None:
    zone("UTC")

    assert user_message("deploy it", ARRIVED) == {
        "role": "user", "content": "deploy it", "stamp": ARRIVED_STAMP,
    }


def test_a_tool_message_keeps_its_result_raw_and_is_stamped_when_built() -> None:
    message = tool_message("c1", "echo: hi")

    assert message["role"] == "tool"
    assert message["tool_call_id"] == "c1"
    assert message["content"] == "echo: hi"
    assert is_recent(message[STAMP_KEY])


def test_a_message_remembers_when_it_arrived_but_that_is_not_its_identity() -> None:
    before = time.time()
    first = Message("cli", "owner", "cli:t", "hi")

    assert before <= first.at <= time.time()
    assert first == Message("cli", "owner", "cli:t", "hi", at=0.0)


# ---- for_model: the one place a stamp joins the content ---------------------


def test_for_model_folds_the_stamp_in_front_and_drops_its_key() -> None:
    stored = {"role": "user", "content": "deploy it", STAMP_KEY: ARRIVED_STAMP}

    assert for_model([stored]) == [
        {"role": "user", "content": f"{ARRIVED_STAMP} deploy it"}
    ]
    assert stored["content"] == "deploy it"  # what is stored stays what was said


def test_for_model_passes_unstamped_messages_through() -> None:
    legacy = [{"role": "user", "content": "from before stamps"},
              {"role": "assistant", "content": "ok"}]

    assert for_model(legacy) == legacy


# ---- the loop ---------------------------------------------------------------


async def test_the_loop_stores_a_tool_result_raw_and_sends_it_stamped() -> None:
    provider = FakeProvider(
        [tool_turn("echo", {"text": "hi"}, "c1"), text_turn("done")]
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": "x"}]

    await run_turn(
        provider=provider, model="m", messages=messages, tools=echo_registry(),
        on_delta=noop_delta,
    )

    assert messages[2]["content"] == "echo: hi"
    assert is_recent(messages[2][STAMP_KEY])
    assert split_stamp(provider.calls[1][-1]["content"]) == (
        messages[2][STAMP_KEY], "echo: hi",
    )


# ---- the session: user messages ---------------------------------------------


async def test_a_user_turn_is_stored_raw_and_sent_to_the_model_stamped(
    store: MessageStore, zone: Callable[[str], None]
) -> None:
    zone("UTC")
    provider = FakeProvider([text_turn("on it")])
    session = await make_manager(provider, store).get_or_create("cli:t", "cli")

    await session.run_turn("deploy it", noop_delta, at=ARRIVED)

    assert provider.calls[0][1] == {
        "role": "user", "content": f"{ARRIVED_STAMP} deploy it",
    }
    user, reply = await store.load("cli:t")
    assert user == {"role": "user", "content": "deploy it", "stamp": ARRIVED_STAMP}
    assert reply == {"role": "assistant", "content": "on it"}


async def test_the_model_gets_turn_one_byte_identical_on_turn_two(
    store: MessageStore, zone: Callable[[str], None]
) -> None:
    zone("America/New_York")
    provider = FakeProvider([text_turn("one"), text_turn("two")])
    session = await make_manager(provider, store).get_or_create("cli:t", "cli")
    await session.run_turn("first", noop_delta)

    zone("Asia/Kolkata")  # the host's zone moves on; turn one's stamp must not
    await session.run_turn("second", noop_delta)

    before, after = provider.calls[0][1], provider.calls[1][1]
    assert json.dumps(after) == json.dumps(before)
    assert before["content"].endswith("] first")
    assert " IST, UTC+05:30] second" in provider.calls[1][3]["content"]


async def test_a_resumed_thread_sends_the_stored_stamps_unchanged(
    store: MessageStore, zone: Callable[[str], None]
) -> None:
    zone("America/New_York")
    first = FakeProvider([text_turn("one")])
    session = await make_manager(first, store).get_or_create("cli:t", "cli")
    await session.run_turn("before the restart", noop_delta)

    zone("Asia/Kolkata")
    second = FakeProvider([text_turn("two")])
    resumed = await make_manager(second, store).get_or_create("cli:t", "cli")
    await resumed.run_turn("after the restart", noop_delta)

    assert second.calls[0][1] == first.calls[0][1]


async def test_a_queued_message_is_stamped_when_it_arrived_not_when_its_turn_ran(
    store: MessageStore, zone: Callable[[str], None]
) -> None:
    zone("UTC")
    provider = FakeProvider([text_turn("one"), text_turn("two")])
    provider.gate = asyncio.Event()
    dispatcher = Dispatcher(manager_for(provider, store))
    dispatcher.register(RecordingAdapter())
    first = Message("cli", "owner", "cli:t", "start", at=ARRIVED - 240)
    queued = Message("cli", "owner", "cli:t", "and this", at=ARRIVED)

    turn = asyncio.create_task(dispatcher.handle(first))
    await provider.entered.wait()  # the first turn holds the thread
    waiting = asyncio.create_task(dispatcher.handle(queued))
    provider.gate.set()
    await asyncio.gather(turn, waiting)

    stamps = [m["stamp"] for m in await store.load("cli:t") if m["role"] == "user"]
    assert stamps == ["[2026-11-12 23:54:00 UTC, UTC+00:00]", ARRIVED_STAMP]


async def test_a_message_refused_for_budget_is_stamped_like_any_other(
    engine: AsyncEngine, store: MessageStore, zone: Callable[[str], None]
) -> None:
    zone("UTC")
    budget = make_budget(engine, cap=1.0)
    await budget.record("t", 2.0)
    manager = make_budget_manager(FakeProvider([]), store, budget)
    session = await manager.get_or_create("cli:t", "cli")

    result = await session.run_turn("hi", noop_delta, at=ARRIVED)

    assert "budget exhausted" in result.text
    refused, _refusal = await store.load("cli:t")
    assert refused == {"role": "user", "content": "hi", "stamp": ARRIVED_STAMP}


async def test_a_tool_result_filled_in_after_a_crash_is_stamped_like_any_other(
    store: MessageStore,
) -> None:
    call = {
        "id": "c1", "type": "function",
        "function": {"name": "slow", "arguments": "{}"},
    }
    await store.ensure_session("cli:t", "cli")
    await store.append(
        "cli:t",
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [call]},
        ],
    )

    await make_manager(FakeProvider([]), store).get_or_create("cli:t", "cli")

    repaired = (await store.load("cli:t"))[-1]
    assert repaired["tool_call_id"] == "c1"
    assert repaired["content"].startswith("error: interrupted before this tool call")
    assert is_recent(repaired[STAMP_KEY])


# ---- mid-turn injections ----------------------------------------------------


async def test_an_injected_message_is_stamped_when_it_arrived_not_at_the_tool_boundary(
    store: MessageStore, zone: Callable[[str], None]
) -> None:
    zone("UTC")
    gate = Gate()
    provider = FakeProvider([tool_turn("wait", {}), text_turn("switching")])
    session = await manager_for(provider, store, gate.registry()).get_or_create(
        "cli:t", "cli"
    )
    turn = asyncio.create_task(session.run_turn("start", noop_delta))
    await gate.entered.wait()
    assert session.inject(replace(item("change course", []), at=ARRIVED)) is True
    gate.release.set()
    await turn

    assert provider.calls[1][-1] == {
        "role": "user", "content": f"{ARRIVED_STAMP} change course",
    }
    stored = [m for m in await store.load("cli:t") if m["role"] == "user"]
    assert stored[-1] == {
        "role": "user", "content": "change course", "stamp": ARRIVED_STAMP,
    }


async def test_try_inject_carries_the_messages_arrival_into_the_inbox(
    store: MessageStore, zone: Callable[[str], None]
) -> None:
    zone("UTC")
    gate = Gate()
    provider = FakeProvider([tool_turn("wait", {}), text_turn("noted")])
    dispatcher = Dispatcher(manager_for(provider, store, gate.registry()))
    dispatcher.register(RecordingAdapter())
    turn = asyncio.create_task(
        dispatcher.handle(Message("cli", "owner", "cli:t", "start"))
    )
    await gate.entered.wait()

    sent = Message("cli", "owner", "cli:t", "also this", at=ARRIVED)
    assert dispatcher.try_inject(sent, lambda _message, _ticket: None) is True
    gate.release.set()
    await turn

    assert provider.calls[1][-1]["content"] == f"{ARRIVED_STAMP} also this"


# ---- replays ----------------------------------------------------------------


def test_a_replayed_message_keeps_the_time_it_arrived(tmp_path: Path) -> None:
    InflightLog(tmp_path).track("cli:t", "cli", "owner", "queued before the restart")
    arrived = time.time() - 40 * 60  # sat in the record 40 minutes
    path = tmp_path / INFLIGHT_FILE
    record = json.loads(path.read_text())
    for entry in record.values():
        entry["at"] = arrived
    path.write_text(json.dumps(record))

    [interrupted] = InflightLog(tmp_path).take_interrupted()
    [resume] = resume_plan([interrupted])["cli:t"]

    assert interrupted.at == arrived
    assert resume.message.at == arrived


# ---- subagents --------------------------------------------------------------


async def test_a_subagent_stamps_its_messages_the_same_idle_or_mid_turn(
    tmp_path: Path,
) -> None:
    agent = AgentDef(
        name="researcher", description=".", system_prompt="Go.", tools=None,
        model=None,
    )
    provider = FakeProvider([text_turn("one"), text_turn("two"), text_turn("three")])
    provider.gate = asyncio.Event()
    pool = SubagentPool(
        RunDeps(provider, "m", asyncio.Semaphore(1)),
        lambda parent, channel, run_id: ToolRegistry(),
        lambda to, channel, text: None,
        AuditLog(tmp_path / "audit.jsonl"),
        idle_timeout=60.0,
    )
    try:
        await pool.spawn("cli:a", "cli", agent, "dig in")
        await provider.entered.wait()  # its first turn is at the model
        [run] = pool.live()
        run.messaged_parent = True  # it idles after a turn instead of finishing
        pool.deliver(run.id, "mid-turn note")
        provider.gate.set()
        await until(lambda: not run.running)  # took the note, answered, now idle
        pool.deliver(run.id, "idle note")
        await until(lambda: len(provider.calls) == 3)  # woke and ran again
    finally:
        await pool.aclose()

    notes = [m for m in run.messages if m["role"] == "user"]
    assert [m["content"] for m in notes] == ["dig in", "mid-turn note", "idle note"]
    assert all(is_recent(m[STAMP_KEY]) for m in notes)
    sent = provider.calls[-1]
    assert [split_stamp(sent[i]["content"])[1] for i in (1, 3, 5)] == [
        "dig in", "mid-turn note", "idle note",
    ]
