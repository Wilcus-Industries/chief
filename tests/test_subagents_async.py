"""Async, two-way subagents through the real wiring (PRD #308).

Each session and the subagent get their own FakeProvider route: the subagent's
is keyed on its definition prompt, which only its own system message carries.
Parent routes are listed latest-first, since each turn's transcript holds every
marker before it. Run ids are pinned (``new_run_id`` patched) so a parent's
script can address its subagent.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from chief.agent.subagent_notices import take_notices, write_notices
from chief.agent.subagent_pool import SubagentPool
from chief.app import App, build_app
from chief.entrypoint import deliver_subagent_notices
from chief.selfedit.recovery import RestartController

from .fakes import CapturingAdapter, FakeProvider, text_turn, tool_turn
from .test_app import boot, make_config, shutdown
from .test_peers import Ticks, owner, running, user_texts

SUB = "sub:cli:a:abc123"
SUB_PROMPT = "You are a focused researcher."
AGENT_MD = f"""---
name: researcher
description: Reads things and summarizes.
---
{SUB_PROMPT}
"""
# The subagent's call after its parent answered "2024" (as json.dumps shows the
# header's newline), so a test can hold that call until the parent is idle.
ANSWERED = r"from session cli:a]\n2024"


@pytest.fixture(autouse=True)
def _pinned_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chief.agent.subagent_pool.new_run_id", lambda parent: f"sub:{parent}:abc123"
    )


def spawn(task: str = "dig into it", **extra: Any) -> list[Any]:
    return tool_turn("spawn_agent", {"name": "researcher", "task": task, **extra})


@asynccontextmanager
async def daemon(
    tmp_path: Path, sock_path: Path, provider: FakeProvider, **overrides: Any
) -> AsyncIterator[tuple[App, CapturingAdapter, Ticks]]:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "researcher.md").write_text(AGENT_MD)
    async with running(
        tmp_path, sock_path, provider, agents_dir=agents, **overrides
    ) as (app, _device, _web):
        cli = CapturingAdapter("cli")
        app.dispatcher.register(cli)
        yield app, cli, Ticks(app)


def chief_said(cli: CapturingAdapter) -> list[str]:
    return [text for _t, text in cli.sent if not text.startswith("⚙")]


def sub_calls(provider: FakeProvider) -> list[list[dict[str, Any]]]:
    return [c for c in provider.calls if SUB_PROMPT in json.dumps(c)]


def parent_calls(provider: FakeProvider, marker: str) -> list[list[dict[str, Any]]]:
    return [c for c in provider.calls
            if marker in json.dumps(c) and SUB_PROMPT not in json.dumps(c)]


def tool_results(call: list[dict[str, Any]]) -> list[str]:
    """Tool results minus the core screen's wrapper: ``spawn_agent`` stays a
    screened tool, and with no screen backend in tests it tags results
    UNSCREENED rather than passing them bare."""
    return [str(m["content"]).split("</hook>\n\n")[-1]
            for m in call if m["role"] == "tool"]


async def until(predicate: Any) -> None:
    async def loop() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(loop(), timeout=5)


def pool(app: App) -> SubagentPool:
    assert app.peers is not None
    return app.peers.pool


async def test_spawn_returns_at_once_and_the_result_arrives_as_a_message(
    tmp_path: Path, sock_path: Path
) -> None:
    hold = asyncio.Event()
    provider = FakeProvider([])
    provider.route(SUB_PROMPT, [text_turn("findings: 42")], hold=hold)
    provider.route("from subagent", [text_turn("the answer is 42")])
    provider.route("WHILE-WAITING", [text_turn("still here")])
    provider.route("SPAWN-IT", [spawn(), text_turn("spawned it")])
    async with daemon(tmp_path, sock_path, provider) as (app, cli, ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "SPAWN-IT"))
        spawned = tool_results(parent_calls(provider, "SPAWN-IT")[1])[0]
        assert spawned.startswith(f"spawned {SUB} (researcher)")
        # The subagent is still mid-turn, yet the parent answers the owner.
        await app.dispatcher.handle(owner("cli", "cli:a", "WHILE-WAITING"))
        assert chief_said(cli) == ["spawned it", "still here"]
        hold.set()
        await ticks.wait("cli:a", 3)
        assert chief_said(cli)[-1] == "the answer is 42"
        result = (await user_texts(app, "cli:a"))[-1]
        assert f"from subagent {SUB} (researcher)]" in result
        assert "findings: 42" in result
        assert pool(app).live() == []
        # The subagent ran under the core preamble plus its own prompt.
        system = sub_calls(provider)[0][0]["content"]
        assert "send_msg" in system and SUB_PROMPT in system


async def test_question_answer_then_finish(tmp_path: Path, sock_path: Path) -> None:
    resume = asyncio.Event()  # pins the result to after the parent's answer turn
    provider = FakeProvider([])
    provider.route(ANSWERED, [tool_turn("finish", {"result": "the 2024 numbers"})],
                   hold=resume)
    go = asyncio.Event()
    provider.route(SUB_PROMPT, [
        tool_turn("send_msg", {"text": "which year?"}),
        text_turn("waiting on my parent"),
    ], hold=go)
    provider.route("the 2024 numbers", [text_turn("report: the 2024 numbers")])
    provider.route("which year?", [
        tool_turn("send_msg", {"to": SUB, "text": "2024"}), text_turn("answered"),
    ])
    provider.route("ASK-FLOW", [spawn(), text_turn("spawned")])
    async with daemon(tmp_path, sock_path, provider) as (app, cli, ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "ASK-FLOW"))
        go.set()
        await ticks.wait("cli:a", 2)
        resume.set()
        await ticks.wait("cli:a", 3)
        assert chief_said(cli) == ["spawned", "answered", "report: the 2024 numbers"]
        assert len(sub_calls(provider)) == 3  # finish costs no further call
        assert pool(app).live() == []


async def test_wait_mode_returns_early_on_a_question(
    tmp_path: Path, sock_path: Path
) -> None:
    resume = asyncio.Event()
    provider = FakeProvider([])
    provider.route(ANSWERED, [tool_turn("finish", {"result": "done for 2024"})],
                   hold=resume)
    provider.route(SUB_PROMPT, [
        tool_turn("send_msg", {"text": "which year?"}), text_turn("waiting"),
    ])
    provider.route("done for 2024", [text_turn("got the result")])
    provider.route("WAIT-FLOW", [
        spawn(wait=True),
        tool_turn("send_msg", {"to": SUB, "text": "2024"}, call_id="c2"),
        text_turn("answered it"),
    ])
    async with daemon(tmp_path, sock_path, provider) as (app, cli, ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "WAIT-FLOW"))
        resume.set()
        await ticks.wait("cli:a", 2)
        interrupt = tool_results(parent_calls(provider, "WAIT-FLOW")[1])[0]
        assert interrupt == (
            f"subagent {SUB} asks: which year? — still running, reply with "
            f"send_msg(to={SUB})"
        )
        assert chief_said(cli) == ["answered it", "got the result"]


async def test_an_idle_subagent_is_killed_and_the_parent_told(
    tmp_path: Path, sock_path: Path
) -> None:
    provider = FakeProvider([])
    go = asyncio.Event()
    provider.route(SUB_PROMPT, [
        tool_turn("send_msg", {"text": "which year?"}), text_turn("halfway there"),
    ], hold=go)
    provider.route("killed: idle", [text_turn("it timed out")])
    provider.route("which year?", [text_turn("not answering")])
    provider.route("IDLE-FLOW", [spawn(task="the idle task"), text_turn("spawned")])
    async with daemon(tmp_path, sock_path, provider) as (app, cli, ticks):
        pool(app).idle_timeout = 0.05
        await app.dispatcher.handle(owner("cli", "cli:a", "IDLE-FLOW"))
        go.set()
        await ticks.wait("cli:a", 3)
        notice = (await user_texts(app, "cli:a"))[-1]
        assert "killed: idle" in notice
        assert "task: the idle task" in notice
        assert "last progress: halfway there" in notice
        assert chief_said(cli)[-1] == "it timed out"
        assert pool(app).live() == []


async def test_owner_tasks_and_cancel_notify_the_parent(
    tmp_path: Path, sock_path: Path
) -> None:
    hold = asyncio.Event()
    provider = FakeProvider([])
    provider.route(SUB_PROMPT, [text_turn("never seen")], hold=hold)
    provider.route("cancelled by owner", [text_turn("ok, dropped it")])
    provider.route("CANCEL-FLOW", [spawn(task="a long job"), text_turn("spawned")])
    async with daemon(tmp_path, sock_path, provider) as (app, cli, ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "CANCEL-FLOW"))
        await until(lambda: sub_calls(provider))
        await app.dispatcher.handle(owner("cli", "cli:z", "/tasks"))
        thread, listing = cli.sent[-1]
        assert thread == "cli:z"
        assert SUB in listing and "cli:a" in listing
        assert "researcher" in listing and "running" in listing
        assert "a long job" in listing
        await app.dispatcher.handle(owner("cli", "cli:z", f"/cancel {SUB}"))
        assert cli.sent[-1] == ("cli:z", f"cancelled {SUB}")
        await ticks.wait("cli:a", 2)
        assert "cancelled by owner" in (await user_texts(app, "cli:a"))[-1]
        assert chief_said(cli)[-1] == "ok, dropped it"
        await app.dispatcher.handle(owner("cli", "cli:z", "/cancel sub:nope"))
        assert cli.sent[-1] == ("cli:z", "no running subagent sub:nope")
        await app.dispatcher.handle(owner("cli", "cli:z", "/tasks"))
        assert cli.sent[-1] == ("cli:z", "no subagents running")


async def test_parent_lists_and_cancels_its_own_subagents(
    tmp_path: Path, sock_path: Path
) -> None:
    hold = asyncio.Event()
    provider = FakeProvider([])
    provider.route(SUB_PROMPT, [text_turn("never seen")], hold=hold)
    provider.route("TOOL-FLOW", [
        spawn(task="background job"),
        tool_turn("spawn_agent", {"action": "list"}, call_id="c2"),
        tool_turn("spawn_agent", {"action": "cancel", "id": SUB}, call_id="c3"),
        text_turn("cancelled it"),
    ])
    async with daemon(tmp_path, sock_path, provider) as (app, cli, _ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "TOOL-FLOW"))
        last = parent_calls(provider, "TOOL-FLOW")[-1]
        results = tool_results(last)
        assert SUB in results[1] and "background job" in results[1]
        assert results[2] == f"cancelled {SUB}"
        # Cancel waits for the run to wind down, so its notice reached the
        # still-running parent turn at that turn's next tool step.
        assert "cancelled by parent" in str(last[-1]["content"])
        assert chief_said(cli) == ["cancelled it"]
        assert pool(app).live() == []


async def test_a_session_cannot_cancel_or_message_another_sessions_subagent(
    tmp_path: Path, sock_path: Path
) -> None:
    hold = asyncio.Event()
    provider = FakeProvider([])
    provider.route(SUB_PROMPT, [text_turn("done")], hold=hold)
    provider.route("B-FLOW", [
        tool_turn("spawn_agent", {"action": "cancel", "id": SUB}),
        tool_turn("send_msg", {"to": SUB, "text": "hi"}, call_id="c2"),
        text_turn("tried"),
    ])
    provider.route("A-FLOW", [spawn(), text_turn("spawned")])
    async with daemon(tmp_path, sock_path, provider) as (app, _cli, _ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "A-FLOW"))
        await app.dispatcher.handle(owner("cli", "cli:b", "B-FLOW"))
        results = tool_results(parent_calls(provider, "B-FLOW")[-1])
        assert results[0] == f"error: no running subagent of yours named '{SUB}'"
        assert results[1] == f"error: no live subagent of yours named '{SUB}'"
        assert [r.id for r in pool(app).live()] == [SUB]


async def test_subagent_may_only_message_its_parent(
    tmp_path: Path, sock_path: Path
) -> None:
    provider = FakeProvider([])
    go = asyncio.Event()
    provider.route(SUB_PROMPT, [
        tool_turn("send_msg", {"to": "cli:b", "text": "psst"}), text_turn("fine"),
    ], hold=go)
    provider.route("from subagent", [text_turn("ok")])
    provider.route("SCOPE-FLOW", [spawn(), text_turn("spawned")])
    async with daemon(tmp_path, sock_path, provider) as (app, _cli, ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "SCOPE-FLOW"))
        go.set()
        await ticks.wait("cli:a", 2)
        refused = tool_results(sub_calls(provider)[1])[0]
        assert refused.startswith("error: a subagent can only message its parent")
        assert await user_texts(app, "cli:b") == []


async def test_clearing_the_parent_cancels_its_subagents_silently(
    tmp_path: Path, sock_path: Path
) -> None:
    hold = asyncio.Event()
    provider = FakeProvider([])
    provider.route(SUB_PROMPT, [text_turn("never seen")], hold=hold)
    provider.route("ORPHAN-FLOW", [spawn(), text_turn("spawned")])
    async with daemon(tmp_path, sock_path, provider) as (app, _cli, _ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "ORPHAN-FLOW"))
        await until(lambda: sub_calls(provider))
        await app.dispatcher.handle(owner("cli", "cli:a", "/clear"))
        await until(lambda: pool(app).live() == [])
        assert await user_texts(app, "cli:a") == []  # no notice: silent
        audit = [json.loads(line) for line in
                 (tmp_path / "audit.jsonl").read_text().splitlines()]
        [cancelled] = [e for e in audit if e["kind"] == "subagent_cancelled"]
        assert cancelled["id"] == SUB and cancelled["parent"] == "cli:a"


async def test_subagent_tool_calls_are_gated_carded_on_the_parent_and_audited(
    tmp_path: Path, sock_path: Path
) -> None:
    create = {"action": "create", "description": "x", "spec": "0 9 * * *",
              "prompt": "hi"}
    provider = FakeProvider([])
    go = asyncio.Event()
    provider.route(SUB_PROMPT, [tool_turn("schedule", create), text_turn("denied")],
                   hold=go)
    provider.route("from subagent", [text_turn("ok")])
    provider.route("GATE-FLOW", [spawn(), text_turn("spawned")])
    async with daemon(tmp_path, sock_path, provider) as (app, cli, ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "GATE-FLOW"))
        go.set()
        await until(lambda: any("approve tool call schedule" in t for _, t in cli.sent))
        card_thread = next(t for t, x in cli.sent if "approve tool call" in x)
        assert card_thread == "cli:a"
        await app.dispatcher.handle(owner("cli", "cli:a", "no"))
        await ticks.wait("cli:a", 2)
        assert await app.cron_service.list_enabled() == []
        entries = [json.loads(line) for line in
                   (tmp_path / "audit.jsonl").read_text().splitlines()]
        [gated] = [e for e in entries if e.get("tool") == "schedule"]
        assert gated["subagent"] == SUB and gated["outcome"] == "card:deny"
        assert gated["thread"] == "cli:a"
        # No per-call announcements for subagent calls.
        assert not any(t.startswith("⚙ schedule") for _, t in cli.sent)


async def test_idle_subagents_hold_no_slot(
    tmp_path: Path, sock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = iter(["sub:cli:a:first1", "sub:cli:a:second"])
    monkeypatch.setattr("chief.agent.subagent_pool.new_run_id", lambda p: next(ids))
    second_task = json.dumps({"role": "user", "content": "SECOND-JOB"})[1:-1]
    provider = FakeProvider([])
    # Keyed on the second run's own task message: the parent's transcript also
    # names the task, but only inside the spawn call's json-encoded arguments.
    first_go, second_go = asyncio.Event(), asyncio.Event()
    provider.route(second_task, [text_turn("second done")], hold=second_go)
    provider.route(SUB_PROMPT, [
        tool_turn("send_msg", {"text": "question from first"}), text_turn("idle now"),
    ], hold=first_go)
    provider.route("second done", [text_turn("ok")])
    provider.route("CAP-TWO", [spawn(task="SECOND-JOB"), text_turn("spawned second")])
    provider.route("question from first", [text_turn("later")])
    provider.route("CAP-FLOW", [spawn(task="FIRST-JOB"), text_turn("spawned first")])
    async with daemon(
        tmp_path, sock_path, provider, subagents_max_concurrent=1
    ) as (app, _cli, ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "CAP-FLOW"))
        first_go.set()
        await ticks.wait("cli:a", 2)  # first asked its question and went idle
        # The idle first run holds no slot, so the second runs to completion.
        await app.dispatcher.handle(owner("cli", "cli:a", "CAP-TWO"))
        second_go.set()
        await ticks.wait("cli:a", 4)
        assert [r.id for r in pool(app).live()] == ["sub:cli:a:first1"]


async def test_restart_kills_subagents_and_the_parent_hears_after_boot(
    tmp_path: Path, sock_path: Path
) -> None:
    hold = asyncio.Event()
    provider = FakeProvider([])
    provider.route(SUB_PROMPT, [text_turn("never seen")], hold=hold)
    provider.route("RESTART-FLOW", [spawn(task="the doomed job"), text_turn("spawned")])
    async with daemon(tmp_path, sock_path, provider) as (app, _cli, _ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "RESTART-FLOW"))
        await until(lambda: sub_calls(provider))
        assert app.peers is not None
        app.peers.pre_restart()
    # A fresh daemon on the same data dir: the notice wakes the parent.
    config = make_config(tmp_path, sock_path)
    app2, streams = await boot(config, FakeProvider([text_turn("will respawn")]))
    try:
        ticks = Ticks(app2)
        await deliver_subagent_notices(app2, config.db_path.parent)
        await ticks.wait("cli:a")
        notice = (await user_texts(app2, "cli:a"))[-1]
        assert f"from subagent {SUB} (researcher)]" in notice
        assert "killed by restart" in notice
        assert "task: the doomed job" in notice
        assert not (config.db_path.parent / "subagent_notices.json").exists()
    finally:
        await shutdown(app2, streams)


async def test_build_app_exposes_the_pool(tmp_path: Path, sock_path: Path) -> None:
    app = await build_app(make_config(tmp_path, sock_path), provider=FakeProvider([]))
    assert app.peers is not None
    assert app.peers.pool.live() == []
    await app.engine.dispose()


async def test_a_subagent_cannot_act_as_its_parent_through_native_tools(
    tmp_path: Path, sock_path: Path
) -> None:
    # The run's context (parent thread + run id) drives gating and audit, but
    # a native tool must not get it: switch_model would re-model the parent,
    # shell would move the parent's persistent cwd.
    provider = FakeProvider([])
    go = asyncio.Event()
    provider.route(SUB_PROMPT, [tool_turn("switch_model", {"model": "acme/other"}),
                                text_turn("tried")], hold=go)
    provider.route("from subagent", [text_turn("ok")])
    provider.route("NATIVE-CTX", [spawn(), text_turn("spawned")])
    async with daemon(tmp_path, sock_path, provider) as (app, cli, ticks):
        await app.dispatcher.handle(owner("cli", "cli:a", "NATIVE-CTX"))
        go.set()
        await until(lambda: any("approve tool call switch_model" in t
                                for _, t in cli.sent))
        await app.dispatcher.handle(owner("cli", "cli:a", "yes"))
        await until(lambda: len(sub_calls(provider)) >= 2)
        result = tool_results(sub_calls(provider)[1])[-1]
        assert result == "error: switch_model needs a session context"
        assert await app.store.model_override("cli:a") is None


async def test_pre_restart_hooks_run_before_the_exec_and_cannot_block_it() -> None:
    order: list[str] = []
    controller = RestartController(lambda: order.append("exec"))

    def broken() -> None:
        raise RuntimeError("disk full")

    controller.add_pre_restart(broken)
    controller.add_pre_restart(lambda: order.append("hook"))
    controller.request()
    await controller.fire_if_requested()
    assert order == ["hook", "exec"]


def test_stale_or_corrupt_notices_are_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = {"parent_key": "cli:a", "parent_channel": "cli", "text": "t"}
    write_notices(tmp_path, [entry])
    assert take_notices(tmp_path) == [entry]
    write_notices(tmp_path, [entry])
    real = time.time()
    monkeypatch.setattr("chief.agent.subagent_notices.time.time", lambda: real + 3600)
    assert take_notices(tmp_path) == []
    (tmp_path / "subagent_notices.json").write_text("{not json")
    assert take_notices(tmp_path) == []
    assert not (tmp_path / "subagent_notices.json").exists()
    write_notices(tmp_path, [])  # nothing live: no file at all
    assert not (tmp_path / "subagent_notices.json").exists()
