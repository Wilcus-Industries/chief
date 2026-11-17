"""Arrival stamps on transcript messages.

A user or tool message is stored with its raw ``content`` and a ``stamp`` beside
it; the stamp is folded in front of the content only when a model is sent the
transcript (``provider.base.for_model``). The stamp names when the message
*arrived* — and, for one that waited past ``BACKLOG_SECONDS``, when it was
read — formatted once and kept, so the model-facing prefix never changes.

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
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Message
from chief.agent.loop import TurnResult, run_turn
from chief.agent.subagent_drive import RunDeps
from chief.agent.subagent_pool import SubagentPool
from chief.agent.timestamps import (
    BACKLOG_SECONDS,
    STAMP_NOTE,
    stamp,
    tool_message,
    user_message,
)
from chief.audit import AuditLog
from chief.dispatch import Dispatcher
from chief.hooks import HookRegistry, TurnContext
from chief.persistence.store import MessageStore
from chief.provider.base import STAMP_KEY, for_model
from chief.selfedit.inflight import InflightLog
from chief.selfedit.resume import resume_plan
from chief.subagents import AgentDef
from chief.tools import ToolRegistry

from .fakes import CapturingAdapter, FakeProvider, text_turn, tool_turn
from .test_budget import make_budget
from .test_budget import make_manager as make_budget_manager
from .test_dispatch import RecordingAdapter
from .test_hooks import make_manager as make_hooked_manager
from .test_imessage import OWNER, Harness, chat_db_date
from .test_inflight import make_dispatcher
from .test_inject import Gate, item, manager_for
from .test_loop import echo_registry
from .test_session import make_manager, noop_delta

pytestmark = pytest.mark.timestamps

LABEL = r"\[(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d) \S+, UTC([+-]\d\d:\d\d)\]"
# A message that arrives just before midnight UTC: the case where stamping the
# moment it is *processed* would put it on the wrong day.
ARRIVED = datetime(2026, 11, 12, 23, 58, tzinfo=UTC).timestamp()
ARRIVED_STAMP = "[2026-11-12 23:58:00 UTC, UTC+00:00]"
# The clock a test starts with: just after ARRIVED, so a message that arrived
# then is fresh and its stamp is the single arrival time.
NOW = ARRIVED + 2


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


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Pin the time ``chief.agent.timestamps`` reads as now, at ``NOW`` until a
    test moves it. A message built long after it arrived also names the moment
    it is built, so a test with a fixed arrival time needs a fixed clock to know
    what its stamp says."""

    def set_now(epoch: float) -> None:
        monkeypatch.setattr(
            "chief.agent.timestamps.time", SimpleNamespace(time=lambda: epoch)
        )

    set_now(NOW)
    return set_now


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


def test_stamp_adds_when_it_was_read_in_the_same_zone(
    zone: Callable[[str], None],
) -> None:
    zone("America/Los_Angeles")
    sent = datetime(2026, 11, 17, 17, 0, 0, tzinfo=UTC).timestamp()
    read = datetime(2026, 11, 17, 18, 30, 12, tzinfo=UTC).timestamp()

    assert stamp(sent, read) == "[2026-11-17 09:00:00 PST, UTC-08:00; read 10:30:12]"


def test_stamp_names_the_day_of_a_read_time_that_falls_on_another_day(
    zone: Callable[[str], None],
) -> None:
    # A bare "read 00:10:00" would pass for the 12th, nearly a day before the
    # message was sent, when it is twenty minutes after.
    zone("UTC")

    label = stamp(ARRIVED - 8 * 60, ARRIVED + 12 * 60)

    assert label == "[2026-11-12 23:50:00 UTC, UTC+00:00; read 2026-11-13 00:10:00]"


def test_stamp_names_the_offset_of_a_read_time_after_the_clocks_change(
    zone: Callable[[str], None],
) -> None:
    # 40 minutes pass, yet the wall clock reads 20 minutes *earlier*: only the
    # offset keeps the read time from being taken for the same one as the send.
    zone("America/New_York")
    sent = datetime(2026, 11, 1, 5, 30, tzinfo=UTC).timestamp()  # 01:30 EDT
    read = datetime(2026, 11, 1, 6, 10, tzinfo=UTC).timestamp()  # 01:10 EST

    assert stamp(sent, read) == (
        "[2026-11-01 01:30:00 EDT, UTC-04:00; read 01:10:00 EST, UTC-05:00]"
    )


# ---- the message builders ---------------------------------------------------


@pytest.mark.usefixtures("clock")
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


@pytest.mark.usefixtures("clock")
def test_a_tool_result_never_carries_a_read_time(zone: Callable[[str], None]) -> None:
    # It is built the moment it lands, so its one time already is now: there is
    # no wait for a read time to describe.
    zone("UTC")

    assert tool_message("c1", "echo: hi")[STAMP_KEY] == (
        "[2026-11-12 23:58:02 UTC, UTC+00:00]"
    )


@pytest.mark.usefixtures("clock")
def test_a_message_that_waited_says_when_it_was_sent_and_when_it_was_read(
    zone: Callable[[str], None],
) -> None:
    zone("UTC")

    message = user_message("remind me in an hour", NOW - 90 * 60)

    assert message == {
        "role": "user",
        "content": "remind me in an hour",
        "stamp": "[2026-11-12 22:28:02 UTC, UTC+00:00; read 23:58:02]",
    }


@pytest.mark.usefixtures("clock")
def test_a_message_that_just_arrived_carries_a_single_stamp(
    zone: Callable[[str], None],
) -> None:
    zone("UTC")

    assert user_message("hi")[STAMP_KEY] == "[2026-11-12 23:58:02 UTC, UTC+00:00]"
    assert user_message("hi", NOW - 5)[STAMP_KEY] == (
        "[2026-11-12 23:57:57 UTC, UTC+00:00]"
    )


@pytest.mark.usefixtures("clock")
@pytest.mark.parametrize(
    ("waited", "says_read"),
    [
        (0.0, False),
        (BACKLOG_SECONDS - 0.1, False),
        (BACKLOG_SECONDS, False),  # at the threshold is not past it
        (BACKLOG_SECONDS + 0.1, True),
        (90 * 60.0, True),
        (3 * 24 * 3600.0, True),
    ],
)
def test_only_a_wait_past_the_threshold_adds_the_read_time(
    zone: Callable[[str], None], waited: float, says_read: bool
) -> None:
    zone("UTC")

    label = user_message("x", NOW - waited)[STAMP_KEY]

    assert ("; read " in label) is says_read


@pytest.mark.usefixtures("clock")
def test_a_message_from_the_future_carries_a_single_stamp(
    zone: Callable[[str], None],
) -> None:
    # A channel's clock running ahead of this one is not a wait.
    zone("UTC")

    assert "read" not in user_message("hi", NOW + 3600)[STAMP_KEY]


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


# ---- the note that explains the stamp to the model --------------------------


def test_the_stamp_note_says_what_each_time_means_and_which_one_is_now() -> None:
    # A message that waited carries two times, and each answers a different
    # question. Counting "tomorrow" from the read time is a day late (sent 23:40,
    # read 07:15 next morning); counting "in an hour" from a send time that is
    # long past schedules into the past (sent 09:00, read 10:30). So a message's
    # relative times count from when it was sent, now is the newest time
    # anywhere, and nothing is scheduled before it.
    assert "; read" in STAMP_NOTE  # the shape a waiting message's stamp takes
    assert "same zone" in STAMP_NOTE
    # Relative times mean what they meant when the message was written. Stamps
    # also go on group, stranger, system and peer messages, so not "the owner".
    assert "count from when it was sent" in STAMP_NOTE
    assert "when the owner sent it" not in STAMP_NOTE
    assert "never from the sent time" not in STAMP_NOTE  # the old anchoring
    # The read time is when chief got to the message: one queued behind a long
    # turn was received at the sent time.
    assert "when chief got to it" in STAMP_NOTE
    assert "when chief received it" not in STAMP_NOTE
    # Now is the newest time, not the last stamp: in a subagent a delivered
    # message can sit after a tool result stamped later.
    assert "newest time in any stamp" in STAMP_NOTE
    assert "latest stamp" not in STAMP_NOTE
    # A time that has passed is acted on now or reported missed, never pushed on.
    assert "Never schedule anything before now" in STAMP_NOTE
    assert "act on it now or say it was missed" in STAMP_NOTE


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


@pytest.mark.usefixtures("clock")
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


async def test_a_backlog_message_reaches_the_model_unchanged_on_every_later_call(
    store: MessageStore, zone: Callable[[str], None], clock: Callable[[float], None]
) -> None:
    zone("America/New_York")
    first = FakeProvider([text_turn("one"), text_turn("two")])
    session = await make_manager(first, store).get_or_create("cli:t", "cli")
    await session.run_turn("remind me in an hour", noop_delta, at=NOW - 90 * 60)

    zone("Asia/Kolkata")  # the zone and the clock move on; neither time may
    clock(NOW + 3600)
    await session.run_turn("and again", noop_delta)
    after_restart = FakeProvider([text_turn("three")])
    resumed = await make_manager(after_restart, store).get_or_create("cli:t", "cli")
    await resumed.run_turn("after the restart", noop_delta)

    backlog = first.calls[0][1]
    assert backlog == {
        "role": "user",
        "content": "[2026-11-12 17:28:02 EST, UTC-05:00; read 18:58:02] "
                   "remind me in an hour",
    }
    assert json.dumps(first.calls[1][1]) == json.dumps(backlog)
    assert json.dumps(after_restart.calls[0][1]) == json.dumps(backlog)
    [stored, *_] = await store.load("cli:t")
    assert stored["content"] == "remind me in an hour"  # the stamp stays beside it


async def queued_pair_stamps(
    store: MessageStore, clock: Callable[[float], None], turn_ends: float
) -> list[str]:
    """Two messages on one thread: the second arrives at ``ARRIVED`` while the
    first's turn is at the model, and runs when that turn ends, at ``turn_ends``.
    Returns both user messages' stamps."""
    provider = FakeProvider([text_turn("one"), text_turn("two")])
    provider.gate = asyncio.Event()
    dispatcher = Dispatcher(manager_for(provider, store))
    dispatcher.register(RecordingAdapter())
    first = Message("cli", "owner", "cli:t", "start", at=ARRIVED - 240)
    queued = Message("cli", "owner", "cli:t", "and this", at=ARRIVED)

    clock(ARRIVED - 239)  # the first message is built just after it arrives
    turn = asyncio.create_task(dispatcher.handle(first))
    await provider.entered.wait()  # the first turn holds the thread
    waiting = asyncio.create_task(dispatcher.handle(queued))
    clock(turn_ends)
    provider.gate.set()
    await asyncio.gather(turn, waiting)

    return [m["stamp"] for m in await store.load("cli:t") if m["role"] == "user"]


async def test_a_queued_message_is_stamped_when_it_arrived_not_when_its_turn_ran(
    store: MessageStore, zone: Callable[[str], None], clock: Callable[[float], None]
) -> None:
    zone("UTC")

    stamps = await queued_pair_stamps(store, clock, turn_ends=NOW)

    assert stamps == ["[2026-11-12 23:54:00 UTC, UTC+00:00]", ARRIVED_STAMP]


async def test_a_message_queued_behind_a_long_turn_also_says_when_its_turn_began(
    store: MessageStore, zone: Callable[[str], None], clock: Callable[[float], None]
) -> None:
    zone("UTC")

    stamps = await queued_pair_stamps(store, clock, turn_ends=ARRIVED + 90)

    assert stamps == [
        "[2026-11-12 23:54:00 UTC, UTC+00:00]",
        "[2026-11-12 23:58:00 UTC, UTC+00:00; read 23:59:30]",
    ]


@pytest.mark.usefixtures("clock")
async def test_an_imessage_polled_after_downtime_says_when_it_was_sent_and_read(
    store: MessageStore, tmp_path: Path, zone: Callable[[str], None]
) -> None:
    zone("UTC")
    sent = NOW - 90 * 60  # chief was down; this poll is the first
    harness = Harness(tmp_path)
    harness.store.add_message(
        OWNER, "remind me in an hour", from_me=1, chat=OWNER, date=chat_db_date(sent)
    )
    provider = FakeProvider([text_turn("will do")])
    dispatcher = Dispatcher(manager_for(provider, store))
    dispatcher.register(CapturingAdapter("imessage"))
    harness.on_deliver = lambda message: dispatcher.handle(message, fire_restart=False)
    adapter = harness.adapter()

    await adapter.poll_once()
    await adapter.drain()

    [user, _reply] = await store.load(OWNER)
    label = "[2026-11-12 22:28:02 UTC, UTC+00:00; read 23:58:02]"
    assert user[STAMP_KEY] == label
    assert provider.calls[0][1]["content"] == f"{label} remind me in an hour"


@pytest.mark.usefixtures("clock")
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


@pytest.mark.usefixtures("clock")
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


async def test_a_message_injected_before_a_long_tool_call_ends_says_when_it_was_read(
    store: MessageStore, zone: Callable[[str], None], clock: Callable[[float], None]
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
    clock(ARRIVED + 90)  # the tool call runs on; the loop takes it at the boundary
    gate.release.set()
    await turn

    assert provider.calls[1][-1] == {
        "role": "user",
        "content": "[2026-11-12 23:58:00 UTC, UTC+00:00; read 23:59:30] "
                   "change course",
    }


@pytest.mark.usefixtures("clock")
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


# ---- hooks ------------------------------------------------------------------


@pytest.mark.usefixtures("clock")
async def test_hooks_see_the_model_view_while_user_text_stays_as_written(
    store: MessageStore, zone: Callable[[str], None]
) -> None:
    zone("UTC")
    contexts: list[TurnContext] = []
    finished: list[list[dict[str, Any]]] = []

    async def before(turn: TurnContext) -> None:
        contexts.append(turn)

    async def after(_result: TurnResult, messages: list[dict[str, Any]]) -> None:
        finished.append(messages)

    hooks = HookRegistry()
    hooks.register_pre_turn("pkg", before)
    hooks.register_post_turn("pkg", after)
    provider = FakeProvider([text_turn("one"), text_turn("two")])
    session = await make_hooked_manager(provider, store, hooks).get_or_create(
        "cli:t", "cli"
    )
    await session.run_turn("first", noop_delta, at=ARRIVED)
    await session.run_turn("second", noop_delta)

    first_as_sent = {"role": "user", "content": f"{ARRIVED_STAMP} first"}
    assert contexts[1].user_text == "second"  # the incoming text, as written
    assert contexts[1].messages[0] == first_as_sent  # the history, as a model sees it
    assert finished[0][0] == first_as_sent


# ---- replays ----------------------------------------------------------------


async def test_a_replayed_message_keeps_the_time_it_arrived_and_says_when_it_was_read(
    store: MessageStore, tmp_path: Path, zone: Callable[[str], None],
    clock: Callable[[float], None],
) -> None:
    zone("UTC")
    # It arrived days before the process that holds it dies: an iMessage polled
    # after downtime. The record must carry that time, not the one it was made.
    held = Message("cli", "owner", "cli:t", "queued before the restart", at=ARRIVED)
    first_boot, _ = make_dispatcher(FakeProvider([]), store, InflightLog(tmp_path))
    first_boot.track(held)

    clock(ARRIVED + 3 * 24 * 3600)  # the replay runs three days after it arrived
    reboot = InflightLog(tmp_path)
    [resume] = resume_plan(reboot.take_interrupted())["cli:t"]
    provider = FakeProvider([text_turn("on it")])
    dispatcher, _ = make_dispatcher(provider, store, reboot)
    await dispatcher.replay(resume.message, reboot.adopt(resume.turn_id))

    assert resume.message.at == ARRIVED
    [replayed, _reply] = await store.load("cli:t")
    assert replayed[STAMP_KEY] == (
        "[2026-11-12 23:58:00 UTC, UTC+00:00; read 2026-11-15 23:58:00]"
    )


# ---- subagents --------------------------------------------------------------


RESEARCHER = AgentDef(
    name="researcher", description=".", system_prompt="Go.", tools=None, model=None,
)


def make_pool(tmp_path: Path, provider: FakeProvider) -> SubagentPool:
    return SubagentPool(
        RunDeps(provider, "m", asyncio.Semaphore(1)),
        lambda parent, channel, run_id: ToolRegistry(),
        lambda to, channel, text: None,
        AuditLog(tmp_path / "audit.jsonl"),
        idle_timeout=60.0,
    )


async def test_a_subagent_is_told_what_the_stamp_is(tmp_path: Path) -> None:
    # Its messages carry stamps like any other, and it never sees the main
    # agent's system prompt: the explanation has to arrive with its own.
    provider = FakeProvider([text_turn("done")])
    pool = make_pool(tmp_path, provider)
    try:
        await pool.spawn("cli:a", "cli", RESEARCHER, "dig in")
        await until(lambda: len(provider.calls) == 1)
    finally:
        await pool.aclose()

    system = provider.calls[0][0]
    assert system["role"] == "system"
    assert STAMP_NOTE in system["content"]
    assert system["content"].endswith("Go.")  # its own prompt still follows


async def test_a_subagent_stamps_its_messages_the_same_idle_or_mid_turn(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([text_turn("one"), text_turn("two"), text_turn("three")])
    provider.gate = asyncio.Event()
    pool = make_pool(tmp_path, provider)
    try:
        await pool.spawn("cli:a", "cli", RESEARCHER, "dig in")
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
