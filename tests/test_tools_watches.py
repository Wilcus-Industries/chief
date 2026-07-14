"""chief's watch tools (chief.tools.watches): create/list/cancel (#165).

Determinism: a fixed ``now`` (12:00 EDT) and owner_tz America/New_York frame the
expiry math, mirroring test_tools_schedule.py.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.imessage import IMessageTaskIO
from chief.persistence import imessage as imessage_repo
from chief.persistence import watches as repo
from chief.tools.watches import GhostSendRefused, WatchFireGate, WatchService
from imessage_helpers import FixtureRunner

NOW = datetime(2026, 6, 4, 16, 0, tzinfo=UTC)  # 12:00 EDT
OWNER = "+15550000001"
MOM = "+15550000002"
STRANGER = "+15550000003"


def _svc(session_factory: async_sessionmaker[AsyncSession]) -> WatchService:
    return WatchService(
        session_factory=session_factory, owner_tz="America/New_York", now=lambda: NOW
    )


def test_tool_names_and_server_name() -> None:
    svc = WatchService(session_factory=None, owner_tz="UTC")  # type: ignore[arg-type]
    assert svc.server_name == "chief_watches"
    assert set(svc.tool_names) == {
        "mcp__chief_watches__create_watch",
        "mcp__chief_watches__list_watches",
        "mcp__chief_watches__cancel_watch",
        "mcp__chief_watches__list_watch_candidates",
        "mcp__chief_watches__confirm_watch_candidate",
    }


async def test_create_watch_defaults_to_14_day_ttl(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "tell mom I'm late"}
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        row = (await repo.list_watches(s))[0]
    assert row.expiry.replace(tzinfo=UTC) == repo.default_expiry(NOW)
    assert row.tone == repo.TONE_REPORT
    assert row.target_handle == "+15550000001"
    assert row.instruction == "tell mom I'm late"


async def test_create_watch_with_local_expiry_stores_utc(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {
            "target_handle": "+15550000001",
            "instruction": "x",
            "expiry": "2026-06-04T20:00:00",  # local midnight-ish, 20:00 EDT
        }
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        row = (await repo.list_watches(s))[0]
    # 20:00 EDT → 00:00 UTC (next day).
    assert row.expiry.replace(tzinfo=UTC) == datetime(2026, 6, 5, 0, 0, tzinfo=UTC)


async def test_create_watch_silent_tone_persisted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "x", "tone": "silent"}
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        row = (await repo.list_watches(s))[0]
    assert row.tone == repo.TONE_SILENT


async def test_create_watch_without_target_handle_creates_unbound_watch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#168: a blank/omitted target_handle is now a deliberate unbound watch —
    it no longer errors (intentional behavior change from #165)."""
    out = await _svc(session_factory)._build_create().handler(
        {"target_handle": "", "instruction": "watch for the plumber"}
    )
    assert out["is_error"] is False
    assert "unknown sender" in out["content"][0]["text"]
    async with session_factory() as s:
        row = (await repo.list_watches(s))[0]
    assert row.target_handle is None
    assert row.instruction == "watch for the plumber"


async def test_create_watch_rejects_empty_instruction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {"target_handle": "+15550000001", "instruction": ""}
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_watches(s) == []


async def test_create_watch_rejects_bad_tone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "x", "tone": "yell"}
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_watches(s) == []


async def test_create_watch_rejects_unparseable_expiry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {
            "target_handle": "+15550000001",
            "instruction": "x",
            "expiry": "next tuesday",
        }
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_watches(s) == []


async def test_create_watch_rejects_past_expiry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {
            "target_handle": "+15550000001",
            "instruction": "x",
            "expiry": "2026-06-04T06:00:00",  # 06:00 EDT < now (12:00 EDT)
        }
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_watches(s) == []


async def test_list_watches_formats_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "tell her I'll be late"}
    )
    out = await svc._build_list().handler({})
    assert out["is_error"] is False
    text = out["content"][0]["text"]
    assert "+15550000001" in text
    assert "tell her I'll be late" in text
    assert "armed" in text
    assert "report" in text


async def test_list_watches_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_list().handler({})
    assert out["is_error"] is False
    assert "No watches" in out["content"][0]["text"]


async def test_cancel_watch_unknown_id_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_cancel().handler({"watch_id": 999})
    assert out["is_error"] is True


async def test_cancel_watch_armed_cancels_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "x"}
    )
    async with session_factory() as s:
        watch_id = (await repo.list_watches(s))[0].id
    out = await svc._build_cancel().handler({"watch_id": watch_id})
    assert out["is_error"] is False
    assert "will never fire" in out["content"][0]["text"]
    async with session_factory() as s:
        row = await repo.get_watch(s, watch_id)
    assert row is not None and row.state == repo.STATE_CANCELLED


async def test_cancel_watch_already_cancelled_is_a_no_op_with_clear_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "x"}
    )
    async with session_factory() as s:
        watch_id = (await repo.list_watches(s))[0].id
    first = await svc._build_cancel().handler({"watch_id": watch_id})
    assert first["is_error"] is False
    second = await svc._build_cancel().handler({"watch_id": watch_id})
    assert second["is_error"] is False
    assert "nothing to cancel" in second["content"][0]["text"]


# ---- unknown-sender confirm flow (#168) -------------------------------------------


async def test_list_watch_candidates_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_list_candidates().handler({})
    assert out["is_error"] is False
    assert "No pending candidates" in out["content"][0]["text"]


async def test_list_watch_candidates_formats_pending_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler({"instruction": "watch for the plumber"})
    async with session_factory() as s:
        watch = (await repo.list_watches(s))[0]
        candidate = await repo.create_candidate(
            s, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    out = await svc._build_list_candidates().handler({})
    assert out["is_error"] is False
    text = out["content"][0]["text"]
    assert f"#{candidate.id}" in text
    assert "+15550000009" in text
    assert f"watch #{watch.id}" in text


async def test_confirm_watch_candidate_yes_binds_and_reports(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler({"instruction": "watch for the plumber"})
    async with session_factory() as s:
        watch = (await repo.list_watches(s))[0]
        candidate = await repo.create_candidate(
            s, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    out = await svc._build_confirm_candidate().handler(
        {"candidate_id": candidate.id, "decision": "yes"}
    )
    assert out["is_error"] is False
    assert f"watch #{watch.id}" in out["content"][0]["text"]
    assert "+15550000009" in out["content"][0]["text"]
    async with session_factory() as s:
        row = await repo.get_watch(s, watch.id)
    assert row is not None and row.target_handle == "+15550000009"


async def test_confirm_watch_candidate_no_rejects(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler({"instruction": "watch for the plumber"})
    async with session_factory() as s:
        watch = (await repo.list_watches(s))[0]
        candidate = await repo.create_candidate(
            s, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    out = await svc._build_confirm_candidate().handler(
        {"candidate_id": candidate.id, "decision": "no"}
    )
    assert out["is_error"] is False
    assert "stays inert" in out["content"][0]["text"]
    async with session_factory() as s:
        row = await repo.get_watch(s, watch.id)
    assert row is not None and row.target_handle is None


async def test_confirm_watch_candidate_unknown_id_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_confirm_candidate().handler(
        {"candidate_id": 999, "decision": "yes"}
    )
    assert out["is_error"] is True


async def test_confirm_watch_candidate_expired_watch_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler(
        {
            "instruction": "watch for the plumber",
            "expiry": "2026-06-04T13:00:00",  # 13:00 EDT, 1h after NOW (12:00 EDT)
        }
    )
    async with session_factory() as s:
        watch = (await repo.list_watches(s))[0]
        candidate = await repo.create_candidate(
            s, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    later = WatchService(
        session_factory=session_factory,
        owner_tz="America/New_York",
        now=lambda: datetime(2026, 6, 4, 18, 0, tzinfo=UTC),  # past the expiry
    )
    out = await later._build_confirm_candidate().handler(
        {"candidate_id": candidate.id, "decision": "yes"}
    )
    assert out["is_error"] is True


# ---- reply_to_watch: the fire round-trip through the real send seam (#167) ---------


def _fire_setup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    fire_gate: WatchFireGate | None = None,
    workspace_dir: Path | None = None,
) -> tuple[WatchService, FixtureRunner]:
    """A WatchService wired with a real guarded IMessageTaskIO send seam.

    ``workspace_dir`` defaults to ``tmp_path / "workspace"`` (created) — the fence
    ``file_path`` replies (#169) must resolve inside.
    """
    runner = FixtureRunner()
    io = IMessageTaskIO(
        runner,
        outbox_dir=str(tmp_path / "outbox"),
        self_dm=True,
        self_handles=frozenset({imessage_repo.normalize_handle(OWNER)}),
        session_factory=session_factory,
        front_desk=OWNER,
    )
    if workspace_dir is None:
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
    svc = WatchService(
        session_factory=session_factory,
        owner_tz="UTC",
        now=lambda: datetime.now(UTC),
        send=io,
        front_desk=OWNER,
        fire_gate=fire_gate,
        workspace_dir=workspace_dir,
    )
    return svc, runner


async def _armed_watch(
    session_factory: async_sessionmaker[AsyncSession], *, tone: str = repo.TONE_REPORT
) -> int:
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=MOM,
            instruction="reply when she asks",
            expiry=datetime.now(UTC) + timedelta(days=1),
            tone=tone,
        )
    return watch.id


def test_reply_to_watch_absent_without_send_seam() -> None:
    svc = WatchService(session_factory=None, owner_tz="UTC")  # type: ignore[arg-type]
    assert set(svc.tool_names) == {
        "mcp__chief_watches__create_watch",
        "mcp__chief_watches__list_watches",
        "mcp__chief_watches__cancel_watch",
        "mcp__chief_watches__list_watch_candidates",
        "mcp__chief_watches__confirm_watch_candidate",
    }


def test_reply_to_watch_present_with_send_seam(tmp_path: Path) -> None:
    svc, _ = _fire_setup(None, tmp_path)  # type: ignore[arg-type]
    assert "mcp__chief_watches__reply_to_watch" in svc.tool_names


async def test_reply_to_watch_report_tone_sends_and_retires(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    svc, runner = _fire_setup(session_factory, tmp_path)
    watch_id = await _armed_watch(session_factory)

    out = await svc._build_reply().handler(
        {"watch_id": watch_id, "text": "running late"}
    )

    assert out["is_error"] is False
    mom = [c for c in runner.jxa_calls if c[-2:] == (MOM, "running late")]
    assert len(mom) == 1  # the reply reaches MOM as the owner (no prefix)
    report = [c for c in runner.jxa_calls if c[-2] == OWNER]
    assert len(report) == 1 and "✅ Replied to" in report[0][-1]
    async with session_factory() as session:
        watch = await repo.get_watch(session, watch_id)
    assert watch is not None and watch.state == repo.STATE_FIRED


async def test_reply_to_watch_keep_watching_leaves_it_armed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    svc, runner = _fire_setup(session_factory, tmp_path)
    watch_id = await _armed_watch(session_factory)

    out = await svc._build_reply().handler(
        {"watch_id": watch_id, "text": "ok", "keep_watching": True}
    )

    assert out["is_error"] is False
    async with session_factory() as session:
        watch = await repo.get_watch(session, watch_id)
    assert watch is not None and watch.state == repo.STATE_ARMED


async def test_reply_to_watch_consumes_clearance_even_when_keep_watching(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """#167 medium: a fire consumes the eval-turn clearance even for keep_watching, so
    one (possibly hijacked) eval turn can't fire the same watch twice — the 'exactly
    one send' AC. The standing watch re-authorizes on its next real inbound."""
    fire_gate = WatchFireGate()
    svc, runner = _fire_setup(session_factory, tmp_path, fire_gate=fire_gate)
    watch_id = await _armed_watch(session_factory)
    fire_gate.authorize(watch_id)

    first = await svc._build_reply().handler(
        {"watch_id": watch_id, "text": "on my way", "keep_watching": True}
    )
    assert first["is_error"] is False
    assert not fire_gate.is_authorized(watch_id)  # clearance spent regardless

    # The watch is still armed, but its clearance is gone: a second fire this turn is
    # refused by the gate, so only one send reached MOM.
    second = await svc._build_reply().handler(
        {"watch_id": watch_id, "text": "again", "keep_watching": True}
    )
    assert second["is_error"] is True
    assert not any(c[-2:] == (MOM, "again") for c in runner.jxa_calls)


async def test_reply_to_watch_silent_tone_posts_no_self_thread_report(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    svc, runner = _fire_setup(session_factory, tmp_path)
    watch_id = await _armed_watch(session_factory, tone=repo.TONE_SILENT)

    await svc._build_reply().handler({"watch_id": watch_id, "text": "ok"})

    assert [c[-2] for c in runner.jxa_calls] == [MOM]  # only the contact, no report


async def test_reply_to_watch_on_inactive_watch_errors_and_sends_nothing(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    svc, runner = _fire_setup(session_factory, tmp_path)
    watch_id = await _armed_watch(session_factory)
    async with session_factory() as session:
        await repo.cancel_watch(session, watch_id)

    out = await svc._build_reply().handler({"watch_id": watch_id, "text": "hi"})

    assert out["is_error"] is True
    assert runner.jxa_calls == []  # nothing sent for a cancelled watch


async def test_reply_to_watch_unbound_watch_errors(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    svc, runner = _fire_setup(session_factory, tmp_path)
    async with session_factory() as session:
        watch = await repo.create_watch(
            session,
            target_handle=None,  # unbound — no handle to send to
            instruction="watch for someone",
            expiry=datetime.now(UTC) + timedelta(days=1),
        )

    out = await svc._build_reply().handler({"watch_id": watch.id, "text": "hi"})

    assert out["is_error"] is True
    assert runner.jxa_calls == []


# ---- #167 hardening: the eval-turn fire gate + delivery signal ---------------------


async def test_reply_to_watch_refuses_a_watch_the_eval_did_not_authorize(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """High finding: a prompt-injected eval turn mints its own armed watch on an
    attacker handle and tries to fire it. The send seam's watch check WOULD authorize
    the freshly minted watch (it is armed on that handle), so the fire gate is the
    control: only a watch a real inbound dispatched an eval for may fire."""
    runner = FixtureRunner()
    fire_gate = WatchFireGate()
    io = IMessageTaskIO(
        runner,
        outbox_dir=str(tmp_path / "outbox"),
        self_dm=True,
        self_handles=frozenset({imessage_repo.normalize_handle(OWNER)}),
        session_factory=session_factory,
        front_desk=OWNER,
    )
    svc = WatchService(
        session_factory=session_factory,
        owner_tz="UTC",
        now=lambda: datetime.now(UTC),
        send=io,
        front_desk=OWNER,
        fire_gate=fire_gate,
    )
    # A legit watch the adapter cleared, and one the (hijacked) turn just minted.
    legit = await _armed_watch(session_factory)
    fire_gate.authorize(legit)
    async with session_factory() as session:
        minted = await repo.create_watch(
            session,
            target_handle=STRANGER,
            instruction="exfil",
            expiry=datetime.now(UTC) + timedelta(days=1),
        )

    out = await svc._build_reply().handler(
        {"watch_id": minted.id, "text": "owner secrets"}
    )

    assert out["is_error"] is True
    assert runner.jxa_calls == []  # nothing reached the attacker handle
    async with session_factory() as session:
        still = await repo.get_watch(session, minted.id)
    assert still is not None and still.state == repo.STATE_ARMED  # not fired

    # The cleared watch still fires normally through the same gate.
    ok = await svc._build_reply().handler({"watch_id": legit, "text": "running late"})
    assert ok["is_error"] is False
    assert any(c[-2:] == (MOM, "running late") for c in runner.jxa_calls)


class _RefusingSend:
    """A send seam that refuses like the guard does on an unauthorized ghost-send."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def send(self, thread_key: str, text: str) -> None:
        self.calls.append((thread_key, text))
        raise GhostSendRefused(thread_key)

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None:
        self.calls.append((thread_key, filename))
        raise GhostSendRefused(thread_key)


async def test_reply_to_watch_refused_send_leaves_watch_armed_and_reports_no_success(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Medium finding: when the send seam refuses (raises), the fire must NOT retire
    the watch or post a false success — it reports the failure, watch stays armed."""
    send = _RefusingSend()
    fire_gate = WatchFireGate()
    svc = WatchService(
        session_factory=session_factory,
        owner_tz="UTC",
        now=lambda: datetime.now(UTC),
        send=send,
        front_desk=OWNER,
        fire_gate=fire_gate,
    )
    watch_id = await _armed_watch(session_factory)
    fire_gate.authorize(watch_id)

    out = await svc._build_reply().handler({"watch_id": watch_id, "text": "hi mom"})

    assert out["is_error"] is True
    assert send.calls == [(MOM, "hi mom")]  # attempted the contact, nothing more
    assert not any(c[0] == OWNER for c in send.calls)  # no ✅ self-thread report
    async with session_factory() as session:
        watch = await repo.get_watch(session, watch_id)
    assert watch is not None and watch.state == repo.STATE_ARMED  # not retired


# ---- reply_to_watch: file replies (#169) -------------------------------------------


async def test_reply_to_watch_file_path_sends_and_retires(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    svc, runner = _fire_setup(session_factory, tmp_path)
    watch_id = await _armed_watch(session_factory)
    form = tmp_path / "workspace" / "form.pdf"
    form.write_bytes(b"%PDF-1.4\nfilled form\n")

    out = await svc._build_reply().handler(
        {
            "watch_id": watch_id,
            "file_path": str(form),
            "text": "here's the filled form",
        }
    )

    assert out["is_error"] is False
    outbox_file = tmp_path / "outbox" / "form.pdf"
    file_calls = [
        c for c in runner.jxa_calls if c[-2:] == (MOM, str(outbox_file.resolve()))
    ]
    assert len(file_calls) == 1
    caption_calls = [
        c for c in runner.jxa_calls if c[-2:] == (MOM, "here's the filled form")
    ]
    assert len(caption_calls) == 1
    report = [c for c in runner.jxa_calls if c[-2] == OWNER]
    assert len(report) == 1 and "file form.pdf" in report[0][-1]
    async with session_factory() as session:
        watch = await repo.get_watch(session, watch_id)
    assert watch is not None and watch.state == repo.STATE_FIRED


async def test_reply_to_watch_file_path_and_text_both_absent_errors(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    svc, runner = _fire_setup(session_factory, tmp_path)
    watch_id = await _armed_watch(session_factory)

    out = await svc._build_reply().handler({"watch_id": watch_id})

    assert out["is_error"] is True
    assert runner.jxa_calls == []


async def test_reply_to_watch_file_path_over_cap_errors(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from chief.tools.watches import _MAX_REPLY_FILE_BYTES

    svc, runner = _fire_setup(session_factory, tmp_path)
    watch_id = await _armed_watch(session_factory)
    big = tmp_path / "workspace" / "big.pdf"
    big.write_bytes(b"x" * (_MAX_REPLY_FILE_BYTES + 1))

    out = await svc._build_reply().handler(
        {"watch_id": watch_id, "file_path": str(big)}
    )

    assert out["is_error"] is True
    assert runner.jxa_calls == []


async def test_reply_to_watch_unreadable_file_path_errors(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    svc, runner = _fire_setup(session_factory, tmp_path)
    watch_id = await _armed_watch(session_factory)

    out = await svc._build_reply().handler(
        {"watch_id": watch_id, "file_path": str(tmp_path / "workspace" / "missing.pdf")}
    )

    assert out["is_error"] is True
    assert runner.jxa_calls == []


async def test_reply_to_watch_file_path_outside_workspace_errors(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """#169 medium finding: an unconfined file_path let a prompt-injected eval turn
    exfiltrate any host-readable file to the watched contact. A path resolving
    outside the configured workspace must be refused before it's ever read."""
    svc, runner = _fire_setup(session_factory, tmp_path)
    watch_id = await _armed_watch(session_factory)
    secret = tmp_path / "secret.txt"  # sibling of tmp_path/workspace, NOT inside it
    secret.write_bytes(b"top secret contents")

    out = await svc._build_reply().handler(
        {"watch_id": watch_id, "file_path": str(secret)}
    )

    assert out["is_error"] is True
    assert runner.jxa_calls == []
    assert "top secret" not in out["content"][0]["text"]
    async with session_factory() as session:
        watch = await repo.get_watch(session, watch_id)
    assert watch is not None and watch.state == repo.STATE_ARMED  # not retired


async def test_reply_to_watch_file_path_traversal_outside_workspace_errors(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Same fence, ``..``-collapsing path — the check runs on the resolved path."""
    svc, runner = _fire_setup(session_factory, tmp_path)
    watch_id = await _armed_watch(session_factory)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top secret contents")
    traversal = tmp_path / "workspace" / ".." / "secret.txt"

    out = await svc._build_reply().handler(
        {"watch_id": watch_id, "file_path": str(traversal)}
    )

    assert out["is_error"] is True
    assert runner.jxa_calls == []


async def test_reply_to_watch_file_path_without_workspace_configured_errors(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """No workspace_dir wired at all — fail closed, never fall back to open reads."""
    svc, runner = _fire_setup(session_factory, tmp_path)
    svc = WatchService(
        session_factory=svc.session_factory,
        owner_tz=svc.owner_tz,
        now=svc.now,
        send=svc.send,
        front_desk=svc.front_desk,
        fire_gate=svc.fire_gate,
        workspace_dir=None,
    )
    watch_id = await _armed_watch(session_factory)
    form = tmp_path / "workspace" / "form.pdf"
    form.write_bytes(b"%PDF-1.4\nfilled form\n")

    out = await svc._build_reply().handler(
        {"watch_id": watch_id, "file_path": str(form)}
    )

    assert out["is_error"] is True
    assert runner.jxa_calls == []
