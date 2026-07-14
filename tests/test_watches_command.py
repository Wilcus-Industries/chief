"""The platform-neutral /watches command (chief.adapters.commands): list + cancel
(#165). Drives CommandContext directly over the Engine protocol boundary, reusing
imessage_helpers.FakeEngine (already Engine-conformant) rather than a new stub.
"""

from datetime import UTC, datetime

from chief.adapters.commands import CommandContext, owner_registry
from chief.persistence.models import Watch
from imessage_helpers import FakeEngine

REGISTRY = owner_registry()


class WatchesFakeEngine(FakeEngine):
    """Adds watch data + cancel-call recording on top of the base FakeEngine."""

    def __init__(self, watches: list[Watch] | None = None) -> None:
        super().__init__()
        self._watches = watches or []
        self.cancelled_watch_ids: list[int] = []

    async def list_watches(self) -> list[Watch]:
        return self._watches

    async def cancel_watch(self, watch_id: int) -> str:
        self.cancelled_watch_ids.append(watch_id)
        return f"Cancelled #{watch_id} — +15550000001 will never fire."


def _watch(**overrides: object) -> Watch:
    # Far in the future so "armed" never flips to "expired" relative to real wall
    # time, however far ahead the test suite happens to run.
    watch = Watch(
        target_handle="+15550000001",
        instruction="tell her I'll be late",
        expiry=datetime(2999, 6, 10, 0, 0, tzinfo=UTC),
    )
    watch.id = 1
    watch.tone = "report"
    watch.state = "armed"
    for key, value in overrides.items():
        setattr(watch, key, value)
    return watch


def _ctx(engine: WatchesFakeEngine, arg: str = "") -> tuple[CommandContext, list[str]]:
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    ctx = CommandContext(
        engine=engine,
        memory=None,
        thread_key="cli:owner",
        arg=arg,
        is_casual=False,
        reply=reply,
    )
    return ctx, replies


async def test_watches_bare_with_no_rows_says_no_watches() -> None:
    engine = WatchesFakeEngine()
    ctx, replies = _ctx(engine)
    await REGISTRY.dispatch("watches", ctx)
    assert replies == ["No watches."]


async def test_watches_bare_lists_target_instruction_expiry_tone_state() -> None:
    engine = WatchesFakeEngine([_watch()])
    ctx, replies = _ctx(engine)
    await REGISTRY.dispatch("watches", ctx)
    assert len(replies) == 1
    text = replies[0]
    assert "+15550000001" in text
    assert "tell her I'll be late" in text
    assert "2999-06-10" in text
    assert "report" in text
    assert "armed" in text


async def test_watches_cancel_calls_engine_and_replies_with_its_result() -> None:
    engine = WatchesFakeEngine()
    ctx, replies = _ctx(engine, arg="cancel 7")
    await REGISTRY.dispatch("watches", ctx)
    assert engine.cancelled_watch_ids == [7]
    assert replies == ["Cancelled #7 — +15550000001 will never fire."]


async def test_watches_cancel_with_no_id_shows_usage() -> None:
    engine = WatchesFakeEngine()
    ctx, replies = _ctx(engine, arg="cancel")
    await REGISTRY.dispatch("watches", ctx)
    assert replies == ["Usage: /watches cancel <id>"]
    assert engine.cancelled_watch_ids == []


async def test_watches_cancel_with_bad_id_shows_usage() -> None:
    engine = WatchesFakeEngine()
    ctx, replies = _ctx(engine, arg="cancel abc")
    await REGISTRY.dispatch("watches", ctx)
    assert replies == ["Usage: /watches cancel <id>"]
    assert engine.cancelled_watch_ids == []
