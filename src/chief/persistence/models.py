"""SQLAlchemy models — the full chief schema (DESIGN: Data model & persistence).

All tables are created up front; only ``contacts`` is read/written in M0/M1. The rest
(``tasks``, ``approvals``, ``policy``, ``rate_limits``, ``schedules``) are shells their
owning milestones (M2/M3/M6/M9) wire behavior onto. ``tier`` is stored as a plain string
so persistence stays independent of the adapter layer.
"""

from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all chief tables."""


class Contact(Base):
    """A known sender, used for tier/admission state and memory namespacing."""

    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("platform", "user_id", name="uq_contact_platform_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str]
    user_id: Mapped[str]
    display_name: Mapped[str | None]
    tier: Mapped[str]
    admitted: Mapped[bool] = mapped_column(default=False)
    # Admission/abuse state (M6): pending → admitted, or blocked/muted by the owner.
    # ``admitted`` is kept for back-compat; ``state`` is authoritative.
    state: Mapped[str] = mapped_column(default="pending")
    namespace: Mapped[str]
    first_seen: Mapped[datetime] = mapped_column(default=_utcnow)


class Task(Base):
    """One conversation = one task/session (lifecycle owned by M2)."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("platform", "thread_key", name="uq_task_platform_thread"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str]
    thread_key: Mapped[str]
    tier: Mapped[str]
    subject_id: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="open")
    model: Mapped[str | None]
    title: Mapped[str | None]
    sdk_session_id: Mapped[str | None]
    #: Per-thread active Google account label (issue #45). None = no account pinned.
    active_account: Mapped[str | None]
    #: Per-task ``/route`` category override (#79). None = auto-classify at spawn.
    route_category: Mapped[str | None]
    #: The thread's real ``adapters.base.Surface`` value (home/dm/group, #135), written
    #: by the first dispatch that knows it and never rewritten afterwards (#151).
    #: ``None`` until then — a task predating this column, or one opened off a path with
    #: no real surface to record. Callers MUST treat ``None`` as "unproven", never
    #: assume DM (a cross-stack inject onto a rebuilt GROUP task otherwise leaks
    #: approval cards into the shared room — see ``adapters.cli._handle_inject``).
    surface: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class Approval(Base):
    """A pending or decided permission-gate request (owned by M3)."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    kind: Mapped[str]
    payload_preview: Mapped[str | None]
    state: Mapped[str] = mapped_column(default="requested")
    decided_by: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    decided_at: Mapped[datetime | None]


class PolicyEntry(Base):
    """A NEVER/APPROVED allowlist entry (safe-matched; owned by M3)."""

    __tablename__ = "policy"

    id: Mapped[int] = mapped_column(primary_key=True)
    list_name: Mapped[str]  # "NEVER" | "APPROVED"
    tool: Mapped[str]
    arg_pattern: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class Route(Base):
    """One job category → routing target (owned by #79, part of #72).

    The row *set* is the category set: every category with a row maps to a
    ``{target_class, model}`` target — ``copilot`` (Copilot quota, e.g. ``auto``) or
    ``openrouter`` (a BYOK per-model target). Seeded from ``config.yaml`` on boot
    (mirrors :class:`PolicyEntry`); the self-config tool (#83) makes both columns and
    the row set itself editable at runtime. ``category`` is unique so a seed / edit
    upserts one target per category. ``description`` is optional free text the
    classifier folds into its prompt so a re-described category steers the next spawn.
    """

    __tablename__ = "routes"
    __table_args__ = (UniqueConstraint("category", name="uq_route_category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str]
    target_class: Mapped[str]  # "copilot" | "openrouter"
    model: Mapped[str]
    description: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class RateLimit(Base):
    """Per-guest and global rate-limit counters (owned by M6)."""

    __tablename__ = "rate_limits"
    # One row per scope: the counter is read-modify-write, so the constraint stops a
    # racing insert from creating a duplicate row (which would later break the
    # single-row read). check_and_increment also serializes under a lock.
    __table_args__ = (UniqueConstraint("scope", name="uq_rate_limit_scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str]  # "global" or a contact namespace
    window_start: Mapped[datetime]
    count: Mapped[int] = mapped_column(default=0)


class UsageMeter(Base):
    """Per-cycle usage in one native currency + its budget mode (#84, part of #72).

    Replaces the single Anthropic-dollar ``monthly_costs`` accumulator: chief now meters
    each turn in the native currency it actually spent — Copilot **premium requests**
    (a raw count vs the 200/mo cap) or **OpenRouter dollars** (metered spend vs a dollar
    cap). No cross-conversion.

    One row per ``(cycle, currency)`` — the cycle key (``"2026-06"``-style) is anchored
    in ``owner_tz``, so a new cycle has no row and the currency resets to 0 implicitly.
    ``amount`` is a read-modify-write accumulator (additive for dollars, or
    monotonic for the premium snapshot), so the unique constraint stops a racing insert
    duplicating a row; the repo also serializes get-or-create under a lock. ``mode`` is
    the per-currency budget state a threshold action or the owner's card decision flips
    (``usage.MODE_*``); ``warned_fraction`` is that currency's last-warned high-water
    mark, so each tier warns once.
    """

    __tablename__ = "usage_meters"
    __table_args__ = (
        UniqueConstraint("cycle", "currency", name="uq_usage_meter_cycle_currency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle: Mapped[str]  # billing-cycle key, e.g. "2026-06"
    currency: Mapped[str]  # usage.PREMIUM_REQUESTS / usage.OPENROUTER_DOLLARS
    amount: Mapped[float] = mapped_column(default=0.0)  # count or dollars per currency
    mode: Mapped[str] = mapped_column(default="normal")  # usage.MODE_* (string const)
    warned_fraction: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class MessageLogEntry(Base):
    """One logged message on a thread — both directions of every stack (#132, #133).

    The per-thread message log and future dashboard read model (#128): every inbound
    owner message and every outbound chief frame lands one row, so a client that
    detaches and reattaches can replay what it missed. Held (undelivered) is not a
    separate outbox — the log *is* the mechanism: an outbound row snapshots
    ``delivered`` from whether any client was attached at emit time, and replay claims
    the undelivered rows on the next attach (restart-proof).

    Persistence stays adapter-independent, so ``role`` is a plain string
    (``messages.ROLE_*``), ``surface`` an adapter ``Surface`` value, and ``kind`` a wire
    frame type — none imported here (same rule as ``tier``). The ``(platform,
    delivered)`` index keeps the replay claim cheap as the log grows.

    Every row is written by one recorder,
    :class:`~chief.persistence.messages.MessageLog` — and each outbound message by
    exactly one caller of it: the CLI stack's ``CliTaskIO`` (#132), the chat stacks'
    broadcast-bus mirror (#133). So ``role`` has one outbound value (``ROLE_CHIEF``) on
    every platform, and a message is never logged twice.

    Columns split by producer, so all three are nullable: the CLI recorder fills
    ``surface`` and ``payload`` (the replay-source wire frame); the mirror fills
    ``filename`` for file rows and leaves ``surface``/``payload`` null.

    ``delivered`` semantics: CLI outbound rows are ``False`` when no client was attached
    at emit and ``True`` otherwise; inbound rows, direct command replies, and the
    payload-less mirror rows are always ``True`` (they reached their destination live,
    or have nothing to re-emit), so a replay claim never touches them.
    """

    __tablename__ = "message_log"
    __table_args__ = (
        Index("ix_message_log_platform_delivered", "platform", "delivered"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str]
    thread_key: Mapped[str]
    role: Mapped[str]  # ROLE_* — "owner"/"chief" (#132) or "assistant" (#133 mirror)
    surface: Mapped[str | None]  # adapters Surface value (#132); None for mirror rows
    kind: Mapped[str]  # wire frame type: user/command/reply/milestone/file
    text: Mapped[str]  # reply/milestone body; the caption ("" if none) for kind="file"
    payload: Mapped[str | None]  # outbound wire-frame JSON (replay src); None inbound
    filename: Mapped[str | None]  # kind="file" only; bytes are NOT persisted
    delivered: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class IMessagePref(Base):
    """Per-handle iMessage conversation prefs (#156): delegation mode + first-send.

    One row per whitelisted handle (a DM thread *is* a handle in v1, so
    per-conversation mode == per-handle mode). ``mode`` is the owner's delegation
    choice for conversations chief conducts with this guest —
    ``imessage.MODE_AUTO`` (reply freely within guest limits) or
    ``imessage.MODE_DRAFT`` (every outbound parks on an approval card first).
    ``contacted`` records whether chief has ever texted the handle: the first send
    to a never-contacted guest handle raises an approval card regardless of mode
    (an outward-facing effect, blacklist-seeded by design).
    """

    __tablename__ = "imessage_prefs"
    __table_args__ = (UniqueConstraint("handle", name="uq_imessage_pref_handle"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    handle: Mapped[str]
    mode: Mapped[str] = mapped_column(default="auto")
    contacted: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class IMessageSend(Base):
    """One own send to a self-handle in self-DM mode (#161): the durable half of
    the loop-proof echo filter.

    In self-DM mode chief's reply to the owner's own handle re-enters the local
    Messages store as an ``is_from_me=0`` row and would re-dispatch forever. Each
    outbound to a self-handle records a row here (``body`` is the exact wire text,
    including the "🤖 " prefix — for a bare file send, the filename); when the echo
    re-polls, the matching row is consumed (deleted) and the row is dropped. The
    record is content-keyed (send returns no ROWID/guid), so it survives a restart.
    Corner: if chief ever sends a file named exactly what the owner later self-texts,
    that owner message is consumed once — astronomically unlikely in a self-chat and
    one-shot, so accepted. No unique constraint/index: single-user, low volume, and
    autogenerate-drift-clean.
    """

    __tablename__ = "imessage_sends"

    id: Mapped[int] = mapped_column(primary_key=True)
    handle: Mapped[str]
    body: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class UnknownSender(Base):
    """Metadata-only log of non-whitelisted senders (#156): handle + timestamps.

    Deliberately content-free — a stranger's text NEVER lands in this table (or in
    any model context); the poller records only who and when, so the owner can ask
    "who's texted you?" and whitelist from there. One row per (platform, handle),
    accumulated in place.
    """

    __tablename__ = "unknown_senders"
    __table_args__ = (
        UniqueConstraint("platform", "handle", name="uq_unknown_platform_handle"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str]
    handle: Mapped[str]
    first_seen: Mapped[datetime] = mapped_column(default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(default=_utcnow)
    count: Mapped[int] = mapped_column(default=0)


class AdapterCursor(Base):
    """A poll adapter's persisted read position (#156): restart-safe incremental
    polling. One row per platform; the iMessage adapter stores the last processed
    ``message.ROWID`` so restarts neither replay old texts nor drop ones that
    arrived while down."""

    __tablename__ = "adapter_cursors"
    __table_args__ = (
        UniqueConstraint("platform", name="uq_adapter_cursor_platform"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str]
    position: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class Schedule(Base):
    """A one-off reminder, a recurring job, or a monitor (owned by M9).

    A ``monitor`` row reuses ``spec`` as its check **cadence** (a cron expression) and
    adds a predicate: each cadence tick checks ``predicate`` (``predicate_type`` picks
    bash-vs-agent) and fires only on a false→true flip (``last_result`` is the last
    evaluated truth, ``None`` until first checked). The predicate columns are NULL for
    ``once``/``recurring`` rows.
    """

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]  # "once" | "recurring" | "monitor" — picks next_run advance logic
    spec: Mapped[str]  # ISO ts ("once"), or a cron expression ("recurring"/"monitor")
    action: Mapped[str | None]  # reminder text, wakeup prompt, or bash command
    action_type: Mapped[str]  # "message" | "wakeup" | "bash" — the fire path
    thread_key: Mapped[str | None]  # delivery/wake target; None ⇒ primary inbox
    urgent: Mapped[bool] = mapped_column(default=False)  # True breaks quiet hours
    next_run: Mapped[datetime | None]
    last_run: Mapped[datetime | None]  # last fire (catch-up + /schedules listing)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    # Monitor-only (NULL for once/recurring): the watched condition + flip-detect state.
    predicate: Mapped[str | None] = mapped_column(default=None)  # bash cmd / agent ask
    predicate_type: Mapped[str | None] = mapped_column(default=None)  # "bash" | "agent"
    last_result: Mapped[bool | None] = mapped_column(default=None)  # None ⇒ never run


class Watch(Base):
    """A standing instruction over one iMessage thread (#165, part of PRD #160).

    Owner-managed access + authorization over a normally-inert non-self thread: the
    owner instructs chief in the self-thread ("if mom texts me today about x, tell
    her y"), chief resolves the target handle via Contacts and records the
    instruction, its expiry (a parsed bound, else the 14-day default), and a
    reporting tone. ``created_at`` is the read-scope floor a later firing milestone
    will enforce (only messages arriving after it ever admit); this slice creates,
    lists, and cancels a watch, and (#170) a per-tick sweep
    (:func:`chief.persistence.watches.sweep_expired`) physically retires an armed
    watch past its expiry to ``expired`` — ``fired`` is still reserved for a later
    milestone. :func:`chief.persistence.watches.effective_state` remains a
    read-time safety net for the gap between real expiry and the next sweep tick.
    """

    __tablename__ = "watches"

    id: Mapped[int] = mapped_column(primary_key=True)
    # None ⇒ unbound: awaiting owner confirmation of an unknown sender (#168).
    target_handle: Mapped[str | None]  # normalized handle (imessage.normalize_handle)
    instruction: Mapped[str]
    tone: Mapped[str] = mapped_column(default="report")  # "report" | "silent"
    state: Mapped[str] = mapped_column(default="armed")  # armed|fired|expired|cancelled
    expiry: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    # Set only when bound via a confirmed candidate (#168) — the admission floor a
    # later dispatch milestone should prefer over created_at when present. None for
    # a directly-created bound watch (#165 path) — zero behavior change there.
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)


class WatchCandidate(Base):
    """A metadata-only sighting against an unbound watch (#168, part of PRD #160).

    One row per (watch_id, handle): the first time an unknown sender's row is seen
    while its watch has no target_handle yet, a candidate is recorded and the owner
    is prompted with handle + timestamp — never content. ``decision`` starts
    "pending"; confirm_watch_candidate flips it to "confirmed" (and binds
    watch.target_handle) or "rejected" (the thread stays inert). Never re-prompted
    once a candidate row exists for that (watch, handle) pair.
    """

    __tablename__ = "watch_candidates"
    __table_args__ = (
        UniqueConstraint("watch_id", "handle", name="uq_watch_candidate_watch_handle"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id"))
    handle: Mapped[str]
    first_seen: Mapped[datetime]
    # pending|confirmed|rejected
    decision: Mapped[str] = mapped_column(default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
