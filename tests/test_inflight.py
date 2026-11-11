"""Turns a restart kills are not lost: held/queued messages replay, cut-off
turns are woken (the restart-resilience follow-ups).

The record is real files under ``tmp_path`` and the turns are real sessions
and a real dispatcher; only the model is the scripted fake. "Process death" is
modelled the way the daemon dies: a held turn is never released (execv), a
running turn is cancelled (asyncio teardown), and a fresh ``InflightLog`` on the
same directory plays the next boot.
"""

import asyncio
import json
import time
from pathlib import Path

from chief.adapters.base import Message
from chief.agent.manager import SessionManager
from chief.dispatch import Dispatcher
from chief.persistence.store import MessageStore
from chief.selfedit.inflight import INFLIGHT_FILE, STALE_SECONDS, InflightLog
from chief.selfedit.recovery import RestartController
from chief.selfedit.resume import RESUME_NOTE, resume_interrupted
from chief.tools import ToolRegistry

from .fakes import CapturingAdapter, FakeProvider, text_turn


async def noop_delta(text: str) -> None:
    pass


def make_manager(
    provider: FakeProvider, store: MessageStore, gate: RestartController
) -> SessionManager:
    return SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="m",
        system_prompt="s",
        max_concurrent=4,
        restart_gate=gate,
    )


def controller(data_dir: Path) -> RestartController:
    return RestartController(lambda: None, inflight=InflightLog(data_dir))


# --- the record itself ------------------------------------------------------


def test_a_finished_turn_leaves_nothing_behind(tmp_path: Path) -> None:
    log = InflightLog(tmp_path)
    ticket = log.track("cli:t", "cli", "owner", "hi")
    ticket.started()
    ticket.done()
    assert InflightLog(tmp_path).take_interrupted() == []


def test_the_next_boot_claims_unfinished_turns_once(tmp_path: Path) -> None:
    log = InflightLog(tmp_path)
    log.track("cli:a", "cli", "owner", "queued one")
    log.track("cli:b", "web", "system", "mid-turn").started()

    reboot = InflightLog(tmp_path)
    # A turn on the fresh boot must not clobber the leftovers before they are
    # taken (the iMessage poller can start a turn before boot resumes them).
    reboot.track("cli:c", "cli", "owner", "new").done()
    taken = reboot.take_interrupted()
    assert [(t.thread_key, t.channel, t.sender, t.text, t.started) for t in taken] == [
        ("cli:a", "cli", "owner", "queued one", False),
        ("cli:b", "web", "system", "mid-turn", True),
    ]
    assert reboot.take_interrupted() == []
    assert InflightLog(tmp_path).take_interrupted() == []


def test_stale_and_corrupt_records_are_dropped(tmp_path: Path) -> None:
    InflightLog(tmp_path).track("cli:a", "cli", "owner", "old")
    path = tmp_path / INFLIGHT_FILE
    data = json.loads(path.read_text())
    for entry in data.values():
        entry["at"] = time.time() - STALE_SECONDS - 1
    path.write_text(json.dumps(data))
    assert InflightLog(tmp_path).take_interrupted() == []

    path.write_text("{not json")
    assert InflightLog(tmp_path).take_interrupted() == []


# --- sessions record what a restart would kill -------------------------------


async def test_a_turn_held_by_a_pending_restart_is_recorded_unstarted(
    store: MessageStore, tmp_path: Path
) -> None:
    gate = controller(tmp_path)
    manager = make_manager(FakeProvider([]), store, gate)
    session = await manager.get_or_create("cli:t", "cli")
    gate.request()
    held = asyncio.create_task(session.run_turn("are you there?", noop_delta))
    await asyncio.sleep(0.05)
    assert not held.done()  # held at the gate; execv would kill it here
    held.cancel()

    [entry] = InflightLog(tmp_path).take_interrupted()
    assert (entry.thread_key, entry.channel, entry.text) == (
        "cli:t", "cli", "are you there?",
    )
    assert entry.started is False
    assert await store.load("cli:t") == []  # never reached history


async def test_a_turn_killed_mid_model_call_is_recorded_started(
    store: MessageStore, tmp_path: Path
) -> None:
    provider = FakeProvider([text_turn("never arrives")])
    provider.gate = asyncio.Event()  # parks the model call
    manager = make_manager(provider, store, controller(tmp_path))
    session = await manager.get_or_create("cli:t", "cli")
    turn = asyncio.create_task(session.run_turn("long job", noop_delta))
    await provider.entered.wait()
    turn.cancel()  # the process going down

    [entry] = InflightLog(tmp_path).take_interrupted()
    assert entry.started is True
    assert (await store.load("cli:t"))[0] == {"role": "user", "content": "long job"}


async def test_completed_and_failed_turns_are_not_resumed(
    store: MessageStore, tmp_path: Path
) -> None:
    manager = make_manager(
        FakeProvider([text_turn("ok")]), store, controller(tmp_path)
    )
    session = await manager.get_or_create("cli:t", "cli")
    await session.run_turn("hi", noop_delta)
    try:
        await session.run_turn("this one fails: the script is empty", noop_delta)
    except Exception:
        pass
    assert InflightLog(tmp_path).take_interrupted() == []


# --- the next boot answers them ---------------------------------------------


async def test_boot_replays_held_messages_and_wakes_cut_off_threads(
    store: MessageStore, tmp_path: Path
) -> None:
    log = InflightLog(tmp_path)
    log.track("cli:cut", "cli", "owner", "do the long job").started()
    log.track("cli:held", "cli", "owner", "first held")
    log.track("cli:held", "cli", "owner", "second held")
    await store.ensure_session("cli:cut", "cli")
    await store.append("cli:cut", [{"role": "user", "content": "do the long job"}])

    provider = FakeProvider([])
    # The held thread's second turn carries the first in its history, so one
    # route serves both calls, in order.
    provider.route("first held", [text_turn("answer one"), text_turn("answer two")])
    provider.route(RESUME_NOTE, [text_turn("picking it back up")])
    dispatcher = Dispatcher(make_manager(provider, store, controller(tmp_path)))
    adapter = CapturingAdapter("cli")
    dispatcher.register(adapter)

    tasks = resume_interrupted(dispatcher, InflightLog(tmp_path).take_interrupted())
    await asyncio.gather(*tasks)

    assert sorted(adapter.sent) == [
        ("cli:cut", "picking it back up"),
        ("cli:held", "answer one"),
        ("cli:held", "answer two"),
    ]
    held = [m["content"] for m in await store.load("cli:held") if m["role"] == "user"]
    assert held == ["first held", "second held"]  # in order, verbatim
    cut = await store.load("cli:cut")
    assert RESUME_NOTE in cut[1]["content"]  # the original message isn't re-sent


async def test_replay_runs_the_turn_without_republishing_or_commands(
    store: MessageStore, tmp_path: Path
) -> None:
    # The held message was already published and command-routed before it
    # was held; replaying must not do either twice.
    provider = FakeProvider([text_turn("ok")])
    dispatcher = Dispatcher(make_manager(provider, store, controller(tmp_path)))
    dispatcher.register(CapturingAdapter("cli"))
    await dispatcher.replay(
        Message(channel="cli", sender="owner", thread_key="cli:t", text="/help")
    )
    assert provider.calls[0][-1] == {"role": "user", "content": "/help"}
