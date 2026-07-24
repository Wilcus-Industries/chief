# SECURITY — trust, the gate, approvals, budget

Read this before changing anything that decides whether a tool runs. Nearly every
rule here is fail-closed on purpose, and several exist because a specific bug got
through once.

## The trust model in one paragraph

Trust is **identity-derived, and identity is decided by the adapter, not by a
central authority**. `sender == "owner"` is the only authorization decision in the
system. Three places mint it:

- `adapters/imessage.py` — owner when the row is in the owner's self-chat or the
  sender matches a configured owner handle.
- `adapters/socket.py` — unconditional. The local Unix socket *is* the trust
  boundary.
- `web/app.py` — unconditional, gated only by the session cookie check.

Everything downstream trusts that string. If you add a channel, you are adding a
fourth place that can mint owner trust — treat it accordingly.

Three sender values matter (`dispatch.py`): `owner`, `system`, and anything else
(a stranger).

## The gate — `gate.py`

**Gating is by tool name only.** Arguments are never inspected for the decision;
they are only rendered into announcement and card text. A tool approved once with
"always" is approved for every argument it will ever receive — an approved `shell`
is an approved *anything*.

`GatedTools.dispatch` decides in strict order:

1. **Unknown tool name** → record `unknown_tool`, pass through to the registry for
   a helpful error. **A hallucinated name must never raise a card**, and must
   never be able to persist an "always" for a tool that doesn't exist.
2. **`policy.decide(name)`** — `never` → NEVER; `"*"` or an exact name in
   `approved` → APPROVED; else ASK. **`never` is checked first and beats the `"*"`
   wildcard.**
3. **ASK + `read_only`** → auto-approved and announced.
4. **ASK otherwise** → approval card. Deliberately *not* announced — the card
   already shows the call, and a second line would double it.
5. **NEVER** → returns an error *string* to the model, not an exception. The turn
   continues and the model sees the denial.

`ASK` is never terminal; it always resolves to NEVER or APPROVED.

### read_only is a narrow promise

`ToolSpec.read_only` means "a pure read — no filesystem write, network, state, or
send". It is the **only** thing that gets auto-approved. Exactly three tools set
it today: `load_skill`, `read_file`, `grep`. `model_tools.py` leaves it False on
purpose and comments why — it is gray by construction, so the gate should ask.

Setting `read_only=True` on a tool is a security change. Treat it as one.

### Persisting "always"

The live set is a mutable `set` on `GatePolicy`. Config-sourced and file-sourced
approvals are unioned at boot, and **only the delta is written back**:
`save_approved(policy.approved - config_approved, path)`. If you drop that
subtraction, config entries get baked into `data/gate_approved.json` and removing
them from config silently stops working.

### Scope gotcha

`GatedTools` is per-session, but `policy`, `audit`, and `approvals` are **shared
singletons**. An "always allow" answered in one thread applies instantly to every
other thread, on every channel.

`dispatch()` ignores its own `context` parameter and always uses `self._context`.
Passing one does nothing.

## Approvals — `approvals.py`

**Cards ride the session's own channel, and mirror to the dashboard (#267).**
`gate.approval_asker`'s `ask` closure resolves `dispatcher.adapter(ctx.channel).send`
so the card appears wherever the conversation is happening, *and* calls the
dispatcher's `tapped()` to push an `approval` frame to any dashboard client
watching that thread — the same tapped-in path a turn's own frames take. The
owner answers by replying normally on the origin channel, or from the
dashboard's card buttons (`POST /approve`), which calls `ApprovalBroker.resolve`
directly — the identical broker a text reply resolves, so first answer wins
regardless of which surface it came from; the loser's call is a no-op (`resolve`
returns `False`, the route reports 409). A client that taps into a thread after
the card was raised still sees it: `GET /events?thread=` replays
`ApprovalBroker.pending_question` as an immediate `approval` frame, and the
`ask` closure emits `approval_resolved` once answered so the dashboard clears it.

**Storage: none — purely in-memory**, keyed by thread (now storing the question
text alongside the future, for that replay). A pending card does not survive a
restart, and since self-edit can `execv`, an in-flight card is silently lost.

Fail-closed, four ways:

- Timeout (`APPROVAL_TIMEOUT_SECONDS`, 600s) → DENY.
- Unparseable answer → DENY.
- A second concurrent card on the same thread → denied outright without even
  being sent. At most one pending card per thread.
- `finally` frees the pending slot on every exit path.

`ALWAYS_ANSWERS` is checked **before** `YES_ANSWERS` so "always" wins rather than
matching as a plain yes. That ordering is load-bearing.

**Careful with `resolve`:** it returns `True` only when it actually consumed the
message — and `True` means *no turn runs*. Make it over-match and owner messages
get silently swallowed. It checks `future.done()` so a second answer to an
already-resolved card falls through into a normal turn.

### The deadlock this module exists to avoid

A turn blocked on a card holds the session lock. So the answer must be consumed
*before* the dispatcher starts a turn — `Dispatcher.handle` calls
`resolve_approval` as its very first statement, ahead of the stranger check and
commands. On a channel with a serialized per-thread worker (iMessage), it must
additionally happen at poll stage **before enqueuing the row**, or the answer
queues behind the very turn it would unblock.

## Strangers — `strangers.py`

There is no screening or scoring in core. The policy is binary and lives in the
dispatcher: sender is neither owner nor system → log, publish, return. **A
stranger never runs a turn** — no model call, no tools, no cost.

`StrangerLog` persists exactly three fields: `channel`, `sender`, `thread_key`.
**Message content is never persisted.** That invariant is stated in three separate
docstrings; keep it.

Note the asymmetry: the bus payload *does* include the text, so stranger content
reaches monitors in memory while never hitting the DB. That is intentional — it
lets notify policy (e.g. a whitelist tier) be agent policy rather than hardcoded.

Group chats are never trusted. A group `sender` stays the raw handle and must
never map to `owner`.

## Audit — `audit.py`

Append-only JSONL at `data/audit.jsonl`. `record(kind, **data)` prepends a UTC
timestamp and kind, serializing with `default=str` so odd values degrade instead
of raising.

The gate emits `kind="tool_call"` with `tool`, `arguments`, `outcome`, `thread`,
`channel`. `outcome` is the security-relevant field, with five shapes:
`unknown_tool`, `read_only`, `list:approved` / `list:never`, and
`card:deny` / `card:once` / `card:always`.

**Every executed or denied call produces exactly one audit line.** Never add a
gate branch that skips `_record`.

Known properties, so they don't surprise you:

- **Full tool arguments are written verbatim — no truncation, no redaction.**
  Secrets passed as tool arguments land in this file in plaintext.
- Synchronous blocking file I/O inside an async dispatch path, open/append/close
  per record.
- No rotation, no size cap, no locking.

## Budget — `budget.py`, `agent/turn_budget.py`

**Per calendar month, global — not per thread.** `thread_key` is recorded on spend
rows but never used for enforcement.

States: `EXHAUSTED` at `spent >= cap`, `WARN` at `spent >= cap * warn_ratio`, else
`OK`. **`cap <= 0` means unlimited**, and the default `budget_cap_usd` is `0.0` —
so the budget is **off by default**.

Enforcement happens once, at the top of `_one_turn`, before the provider call:

- `EXHAUSTED` and no `downgrade_model` → `refuse_over_budget` commits the user
  message and a refusal to the transcript and **never reaches the provider**.
- `EXHAUSTED` with a `downgrade` role model configured → the turn still runs, on
  the cheaper model. With downgrade set, the cap is a *degradation* trigger, not a
  hard stop.

Post-turn, `settle_budget` records cost and attaches a `notice` **only on a
transition into a non-OK state** — so the owner is warned at the OK→WARN and
WARN→EXHAUSTED edges, not every turn.

Two real limits worth knowing:

- **An OAuth-proxy backend reports no dollar cost, so `budget_cap_usd` is inert
  against it.** Zero cost means spend never accrues and the cap never trips. The
  budget only constrains metered (OpenRouter) traffic.
- The check is per-turn, never mid-turn. One turn with a long tool loop can
  overshoot the cap arbitrarily; enforcement re-evaluates only at the next turn.

## Single-instance lock — `instance_lock.py`

Advisory non-blocking `flock` on `<data_dir>/chief.lock`. Prevents two daemons on
one data dir double-polling `chat.db` and answering every iMessage twice.

`flock` was chosen over a PID file because the OS releases it when the holder
exits — SIGKILL, crash, or the self-edit `execv` that replaces the image — so a
dead daemon can never wedge the next boot. No stale-PID liveness dance.

**The easiest way to break this module:** the returned handle must stay alive for
the whole process lifetime. Closing it — or letting it be garbage collected —
releases the lock. `entrypoint.py` assigns it to a local that is deliberately
never used again; the underscore prefix invites a linter or a tidy-up to delete
it, which silently disables the entire guard.

Ordering: acquired after `load_config()` (needed for the path) but before
`build_app` and `app.start()`. Nothing that touches the DB or polls a channel may
run before it. `AlreadyRunning` is caught *before* the generic handler, so a lock
collision does not trigger the self-edit rollback path — a second instance is not
evidence of a bad self-edit.

## Web auth — `web/auth.py`

The only thing standing in front of `web/app.py` minting `sender="owner"`.

One password, one HMAC-SHA256 cookie (`chief_session`), both comparisons via
`hmac.compare_digest`. The secret is persisted at `secrets/web_session_secret`
(mode `0600`) so a restart doesn't log the owner out; rotating that file is the
"log out everywhere" lever.

**Fail-closed twice over:** `Auth.enabled` is `bool(password)` and both
`check_password` and `is_authed` AND against it — no password means nobody can
ever log in. And at wiring, the app is only built `if config.web_password`, so
with no password **no HTTP listener exists at all**.

Default bind is `127.0.0.1:8130` — loopback only. `web_password` is deliberately
**not** read from YAML: only from `CHIEF_WEB_PASSWORD` or `secrets/web_password`.

`/app.css` and `/app.js` are intentionally unauthenticated; every other route
re-checks `is_authed`.

## Scheduled shell commands — `cron/tools.py`, `tools/shell/service.py`

A `command` schedule runs arbitrary shell with nobody present, so two things hold
it:

**Creation is the only control point.** The `schedule` tool raises its own card
before the row exists — independent of the gate's approved list, and refused
outright when no asker is wired. The card renders **both** the command and the
spec through `json.dumps`, so neither can draw its own `yes / no` line and bury
the real payload around it. Creation also parses the spec (`validate_spec`) and
refuses an unparsable one *before* the card — so a forged spec never renders, and
never persists a row that would raise on every tick of the schedule loop. A row
that raises anyway (written by hand, or predating this check) is logged and
skipped per schedule, so it cannot starve the schedules after it.

**Both shell paths carry the same guards.** `shell_guards` (today: the iMessage
`owner_send_guard` echo-loop seatbelt) are wired into the `shell` tool *and*, via
`guarded_runner`, into the unattended runner cron fires. A refused command never
reaches the shell; it returns exit code 126 and is logged. Reaching
`ShellService.run` directly bypasses the seatbelts — always wrap it.

## The invariant list

1. **Fail closed everywhere.** Unparsed approval → deny. Timed-out card → deny.
   Second concurrent card → deny. No web password → no login and no listener.
   Lock contention → refuse to boot.
2. **`never` beats everything**, including `"*"`.
3. **Nothing a stranger sends ever runs a turn**, and **stranger content is never
   persisted**.
4. **Nothing is auto-approved except tools explicitly marked `read_only=True`.**
5. **A hallucinated tool name never raises a card and never persists an "always".**
6. **Every executed or denied call produces exactly one audit line.**
7. **Announcement failure must never break a tool call.** Budget notices and the
   restart report are likewise best-effort.
8. **`system` senders run turns but are never published to the bus** — otherwise
   monitors trigger themselves in a loop.
9. **An unattended shell path is guarded exactly like the owner-driven one**, and
   a control card never renders attacker-shaped text unescaped.

Tests for this layer: `tests/test_gate.py`, `test_approvals.py`,
`test_audit_and_bus.py`, `test_budget.py`, `test_instance_lock.py`,
`test_auth.py`, `test_dispatch.py`.
