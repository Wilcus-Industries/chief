"""Messages a process death kills are not lost: queued, injected and held
messages replay, cut-off turns are woken (the restart-resilience follow-ups).

The record is real files under ``tmp_path``; the turns are real sessions, a
real dispatcher and the real iMessage FIFO; only the model is the scripted
fake. "Process death" is modelled the way the daemon dies: a held turn is
never released (execv), a running turn is cancelled (asyncio teardown), and a
fresh ``InflightLog`` on the same directory plays the next boot.
"""

import asyncio
import json
import time
from pathlib import Path

from chief.adapters.base import Message
from chief.adapters.imessage_fifo import ThreadFifo
from chief.agent.manager import SessionManager
from chief.agent.restart_gate import NullTicket, TurnTicket
from chief.bus import Event, EventBus
from chief.dispatch import Dispatcher
from chief.persistence.store import MessageStore
from chief.selfedit.inflight import INFLIGHT_FILE, STALE_SECONDS, InflightLog
from chief.selfedit.recovery import RestartController
from chief.selfedit.resume import RESUME_NOTE, resume_interrupted, resume_plan
from chief.tools import ToolRegistry

from .fakes import CapturingAdapter, FakeProvider, text_turn


def make_dispatcher(
    provider: FakeProvider, store: MessageStore, log: InflightLog,
    gate: RestartController | None = None, bus: EventBus | None = None,
) -> tuple[Dispatcher, CapturingAdapter]:
    manager = SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="m",
        system_prompt="s",
        max_concurrent=4,
        restart_gate=gate or RestartController(lambda: None),
    )
    dispatcher = Dispatcher(manager, inflight=log, bus=bus)
    adapter = CapturingAdapter("cli")
    dispatcher.register(adapter)
    return dispatcher, adapter


def msg(text: str, sender: str = "owner", thread: str = "cli:t") -> Message:
    return Message(channel="cli", sender=sender, thread_key=thread, text=text)


def leftovers(tmp_path: Path) -> list[tuple[str, bool]]:
    """What the next boot would find: (text, started), oldest first."""
    return [(t.text, t.started) for t in InflightLog(tmp_path).take_interrupted()]


async def boot(
    dispatcher: Dispatcher, log: InflightLog
) -> list[asyncio.Task[None]]:
    """The entrypoint's order: plan and hold, (adapters start), resume."""
    plan = resume_plan(log.take_interrupted())
    dispatcher.hold(list(plan))
    return resume_interrupted(dispatcher, log, plan)


# --- the record itself ------------------------------------------------------


def test_a_finished_message_leaves_nothing_behind(tmp_path: Path) -> None:
    ticket = InflightLog(tmp_path).track("cli:t", "cli", "owner", "hi")
    ticket.started()
    ticket.done()
    assert leftovers(tmp_path) == []


def test_leftovers_stay_recorded_until_their_resume_ends_them(
    tmp_path: Path,
) -> None:
    log = InflightLog(tmp_path)
    log.track("cli:a", "cli", "owner", "queued one")
    log.track("cli:b", "web", "system", "mid-turn").started()

    reboot = InflightLog(tmp_path)
    # A turn on the fresh boot must not clobber the leftovers.
    reboot.track("cli:c", "cli", "owner", "new").done()
    taken = reboot.take_interrupted()
    assert [(t.thread_key, t.channel, t.sender, t.text, t.started) for t in taken] == [
        ("cli:a", "cli", "owner", "queued one", False),
        ("cli:b", "web", "system", "mid-turn", True),
    ]
    assert reboot.take_interrupted() == []  # claimed once per boot
    # This boot dies before resuming anything: nothing is lost.
    assert leftovers(tmp_path) == [("queued one", False), ("mid-turn", True)]

    reboot.adopt(taken[0].turn_id).done()
    assert leftovers(tmp_path) == [("mid-turn", True)]


def test_a_resume_that_dies_too_is_given_up_on(tmp_path: Path) -> None:
    InflightLog(tmp_path).track("cli:t", "cli", "owner", "kills the daemon")
    reboot = InflightLog(tmp_path)
    [turn] = reboot.take_interrupted()
    reboot.adopt(turn.turn_id).started()  # the replay dies again: no done
    assert leftovers(tmp_path) == []  # no crash loop


def test_a_resume_that_never_started_is_not_counted(tmp_path: Path) -> None:
    # Adopted, then held at the gate by another thread's restart: it never
    # ran, so that death is no strike against it.
    InflightLog(tmp_path).track("cli:t", "cli", "owner", "still wanted")
    reboot = InflightLog(tmp_path)
    [turn] = reboot.take_interrupted()
    reboot.adopt(turn.turn_id)
    assert leftovers(tmp_path) == [("still wanted", False)]


def test_age_counts_from_arrival_not_from_the_last_resume(tmp_path: Path) -> None:
    InflightLog(tmp_path).track("cli:t", "cli", "owner", "old")
    path = tmp_path / INFLIGHT_FILE
    data = json.loads(path.read_text())
    for entry in data.values():
        entry["at"] = time.time() - STALE_SECONDS - 1
    path.write_text(json.dumps(data))
    assert leftovers(tmp_path) == []


def test_corrupt_and_malformed_records_never_block_a_boot(tmp_path: Path) -> None:
    path = tmp_path / INFLIGHT_FILE
    InflightLog(tmp_path).track("cli:t", "cli", "owner", "kept")
    data = json.loads(path.read_text())
    data["no-text"] = {"thread_key": "cli:t", "at": time.time()}
    data["bad-at"] = {**next(iter(data.values())), "at": "soon"}
    path.write_text(json.dumps(data))
    assert leftovers(tmp_path) == [("kept", False)]

    for junk in ("{not json", "[]", '"x"'):
        path.write_text(junk)
        assert leftovers(tmp_path) == []


# --- every way chief holds a message is recorded -----------------------------


async def test_a_message_held_by_a_pending_restart_is_recorded_unstarted(
    store: MessageStore, tmp_path: Path
) -> None:
    gate = RestartController(lambda: None)
    log = InflightLog(tmp_path)
    dispatcher, _ = make_dispatcher(FakeProvider([]), store, log, gate)
    gate.request()
    held = asyncio.create_task(dispatcher.handle(msg("are you there?")))
    await asyncio.sleep(0.05)
    assert not held.done()  # held at the gate; execv would kill it here
    held.cancel()

    assert leftovers(tmp_path) == [("are you there?", False)]
    assert await store.load("cli:t") == []  # never reached history


async def test_a_turn_killed_mid_model_call_is_recorded_started(
    store: MessageStore, tmp_path: Path
) -> None:
    provider = FakeProvider([text_turn("never arrives")])
    provider.gate = asyncio.Event()  # parks the model call
    dispatcher, _ = make_dispatcher(provider, store, InflightLog(tmp_path))
    turn = asyncio.create_task(dispatcher.handle(msg("long job")))
    await provider.entered.wait()
    turn.cancel()  # the process going down

    assert leftovers(tmp_path) == [("long job", True)]
    assert (await store.load("cli:t"))[0] == {"role": "user", "content": "long job"}


async def test_the_imessage_backlog_behind_a_held_turn_is_recorded(
    store: MessageStore, tmp_path: Path
) -> None:
    # The cursor is saved at read: queued rows exist nowhere else.
    gate = RestartController(lambda: None)
    log = InflightLog(tmp_path)
    dispatcher, _ = make_dispatcher(FakeProvider([]), store, log, gate)

    async def handle(message: Message, ticket: TurnTicket) -> None:
        await dispatcher.handle(message, fire_restart=False, ticket=ticket)

    fifo = ThreadFifo(handle, track=dispatcher.track)
    gate.request()
    fifo.put(msg("first"))
    fifo.put(msg("second"))
    await asyncio.sleep(0.05)
    await fifo.stop()  # the exec

    assert leftovers(tmp_path) == [("first", False), ("second", False)]


async def test_a_message_injected_into_a_cut_off_turn_is_recorded(
    store: MessageStore, tmp_path: Path
) -> None:
    provider = FakeProvider([text_turn("never arrives")])
    provider.gate = asyncio.Event()
    dispatcher, _ = make_dispatcher(provider, store, InflightLog(tmp_path))
    turn = asyncio.create_task(dispatcher.handle(msg("long job")))
    await provider.entered.wait()
    handed_back: list[Message] = []
    assert dispatcher.try_inject(
        msg("also this"), lambda message, ticket: handed_back.append(message)
    )
    turn.cancel()
    await asyncio.gather(turn, return_exceptions=True)

    # Handed back to a queue path that died with the process: still recorded.
    assert handed_back == [msg("also this")]
    assert leftovers(tmp_path) == [("long job", True), ("also this", False)]


async def test_answered_failed_stranger_and_command_messages_are_not_kept(
    store: MessageStore, tmp_path: Path
) -> None:
    dispatcher, adapter = make_dispatcher(
        FakeProvider([text_turn("ok")]), store, InflightLog(tmp_path)
    )
    commands = RecordingCommands()
    dispatcher.set_commands(commands)
    await dispatcher.handle(msg("hi"))
    await dispatcher.handle(msg("this one fails: the script is empty"))
    await dispatcher.handle(msg("hello?", sender="+15550000"))
    await dispatcher.handle(msg("/help"))
    assert ("cli:t", "error: something went wrong running that turn") in adapter.sent
    assert leftovers(tmp_path) == []


# --- the next boot answers them ---------------------------------------------


async def test_boot_replays_held_messages_and_wakes_cut_off_threads(
    store: MessageStore, tmp_path: Path
) -> None:
    log = InflightLog(tmp_path)
    log.track("cli:cut", "cli", "owner", "do the long job").started()
    log.track("cli:cut", "cli", "owner", "and this too").started()
    log.track("cli:held", "cli", "owner", "first held")
    log.track("cli:held", "cli", "owner", "second held")
    await store.ensure_session("cli:cut", "cli")
    await store.append("cli:cut", [{"role": "user", "content": "do the long job"}])

    provider = FakeProvider([])
    # The held thread's second turn carries the first in its history, so one
    # route serves both calls, in order.
    provider.route("first held", [text_turn("answer one"), text_turn("answer two")])
    provider.route(RESUME_NOTE, [text_turn("picking it back up")])
    reboot = InflightLog(tmp_path)
    dispatcher, adapter = make_dispatcher(provider, store, reboot)
    await asyncio.gather(*await boot(dispatcher, reboot))

    assert sorted(adapter.sent) == [
        ("cli:cut", "picking it back up"),  # one wake for both cut-off turns
        ("cli:held", "answer one"),
        ("cli:held", "answer two"),
    ]
    held = [m["content"] for m in await store.load("cli:held") if m["role"] == "user"]
    assert held == ["first held", "second held"]  # in order, verbatim
    cut = await store.load("cli:cut")
    assert RESUME_NOTE in cut[1]["content"]  # the original message isn't re-sent
    assert leftovers(tmp_path) == []


async def test_a_message_sent_while_down_waits_behind_the_resume(
    store: MessageStore, tmp_path: Path
) -> None:
    InflightLog(tmp_path).track("cli:t", "cli", "owner", "do X")
    reboot = InflightLog(tmp_path)
    provider = FakeProvider([text_turn("did X"), text_turn("ok, stopping")])
    dispatcher, adapter = make_dispatcher(provider, store, reboot)
    plan = resume_plan(reboot.take_interrupted())
    dispatcher.hold(list(plan))
    # The adapters start, and the newer message arrives before the resume.
    newer = asyncio.create_task(dispatcher.handle(msg("never mind")))
    await asyncio.sleep(0.05)
    await asyncio.gather(newer, *resume_interrupted(dispatcher, reboot, plan))

    users = [m["content"] for m in await store.load("cli:t") if m["role"] == "user"]
    assert users == ["do X", "never mind"]
    assert adapter.sent == [("cli:t", "did X"), ("cli:t", "ok, stopping")]


async def test_replay_runs_commands_but_never_republishes(
    store: MessageStore, tmp_path: Path
) -> None:
    # A queued "/help" was recorded before the command stage ever saw it, so
    # the replay must run it as a command; the publish already happened.
    events: list[Event] = []

    async def seen(event: Event) -> None:
        events.append(event)

    bus = EventBus()
    bus.subscribe(seen)
    provider = FakeProvider([text_turn("ok")])
    log = InflightLog(tmp_path)
    dispatcher, adapter = make_dispatcher(provider, store, log, bus=bus)
    commands = RecordingCommands()
    dispatcher.set_commands(commands)
    await dispatcher.replay(msg("/help"), log.track("cli:t", "cli", "owner", "/help"))
    await dispatcher.replay(msg("plain"), NullTicket())

    assert adapter.sent == [("cli:t", "help text"), ("cli:t", "ok")]
    assert [c[-1]["content"] for c in provider.calls] == ["plain"]
    assert events == []
    assert leftovers(tmp_path) == []


async def test_a_resumed_thread_takes_no_injections(
    store: MessageStore, tmp_path: Path
) -> None:
    # "never mind" must queue behind the replays, not jump into the wake.
    provider = FakeProvider([text_turn("resumed")])
    provider.gate = asyncio.Event()
    dispatcher, _ = make_dispatcher(provider, store, InflightLog(tmp_path))
    dispatcher.hold(["cli:t"])
    wake = asyncio.create_task(dispatcher.replay(msg("x", "system"), NullTicket()))
    await provider.entered.wait()
    assert not dispatcher.try_inject(msg("never mind"), lambda m, t: None)
    dispatcher.release("cli:t")
    assert dispatcher.try_inject(msg("never mind"), lambda m, t: None)
    provider.gate.set()
    await wake


async def test_replay_never_runs_a_stranger_turn(
    store: MessageStore, tmp_path: Path
) -> None:
    provider = FakeProvider([text_turn("must not run")])
    dispatcher, adapter = make_dispatcher(provider, store, InflightLog(tmp_path))
    await dispatcher.replay(msg("do it", sender="+15550000"), NullTicket())
    assert provider.calls == []
    assert adapter.sent == []


class RecordingCommands:
    """``/help`` answers like a real command; anything else is not one."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def run(self, message: Message) -> str | Message | None:
        self.seen.append(message.text)
        return "help text" if message.text == "/help" else None
