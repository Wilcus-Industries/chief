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

**Gating is by tool name, plus `ask_when`'s argument *presence*.** Argument
*values* are never inspected for the decision; they are only rendered into
announcement and card text. Outside an `ask_when` rule, a tool approved once
with "always" is approved for every argument it will ever receive — an approved
`shell` is an approved *anything*.

`GatedTools.dispatch` decides in strict order:

1. **Unknown tool name** → record `unknown_tool`, pass through to the registry for
   a helpful error. **A hallucinated name must never raise a card**, and must
   never be able to persist an "always" for a tool that doesn't exist.
2. **`policy.decide(name, arguments)`** — `never` → NEVER. Then, if the call
   carries any argument named by that tool's `ask_when` rule, **the call is
   decided by its grants alone**: every carried watched argument holding a
   `tool:argument` grant → APPROVED, otherwise ASK. The bare name is neither
   required nor sufficient there. Only when no watched argument is present do
   `"*"` or an exact name in `approved` → APPROVED; else ASK. **`never` is
   checked first and beats everything; `ask_when` beats `"*"` and an explicit
   approve.**
3. **ASK + `read_only`** → auto-approved and announced — *unless* the call has
   pending watched arguments. **`ask_when` outranks `read_only`**: the tool may
   only read, but the argument is what makes it act.
4. **ASK otherwise** → approval card. Deliberately *not* announced — the card
   already shows the call, and a second line would double it.
5. **NEVER** → returns an error *string* to the model, not an exception. The turn
   continues and the model sees the denial.

**"always" on an `ask_when` card grants `tool:argument`, not the tool.** That is
why step 2 checks grants before the bare name: a composite grant only lifts the
veto, so consulting the bare name first would make "always" a no-op that re-cards
forever on a tool nothing else approves. The grant is still narrow — it never
approves a call that doesn't carry that argument, and never covers a sibling
argument.

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

## Monitor scope — the prompt-injection boundary (`monitors/`)

A `classifier` predicate sends the event payload — the sender's words, verbatim
— to a model, and that model's verdict decides whether chief wakes. The
stranger's text and the wake instruction share one prompt. **That is a
prompt-injection boundary, not a cost question.**

So: **a classifier-form monitor must declare a scope; a pattern-form monitor
must not.** A `code` predicate is local regex over one field — nothing leaves
the machine, so scanning every message leaks nothing. The asymmetry is the
point; don't "simplify" it into one uniform rule.

- Scope is `{"sender": ...}` or `{"thread_key": ...}` inside the JSON
  `predicate` — exactly one, exact match, compared case-insensitively. No
  normalization: the value must equal the event field as the adapter writes it
  (`+16505551212`, not `650-555-1212`), or the monitor is simply dead.
- `predicate.scope_values()` is the single definition of "has a usable scope",
  so the boot sweep and the runtime check can't disagree about an empty one.
  Blank or whitespace scopes are refused at creation and swept at boot rather
  than becoming monitors that report success and never fire.
- `build_predicate` refuses an unscoped classifier form with an `error: ...`
  string, and refuses a scope on the pattern form.
- `MonitorService._on_event` checks `in_scope` **before** `_matches`. Keep that
  order — after `_matches`, the model has already read the message.
- A classifier row with no usable scope matches nothing (`in_scope` fails
  closed — including a hand-written row scoping on *both* fields), and
  `disable_unscoped()` — run from `App.start` — disables it at boot, logging
  each by id and description at WARNING. `monitor list` shows every row with
  its scope, disabled ones included, so a swept monitor stays visible.

**Scoping to a group trusts every current and future member of that group.** A
group monitor has to scope on `thread_key`, because you cannot name who will
speak: group messages carry the raw handle as `sender` and the chat id as
`thread_key`. Everyone in that chat — including people the owner has never met,
and anyone added later without their involvement — reaches the judge. This is a
documented, accepted exception, not a bug. For 1:1 senders the rule closes the
path completely: an unknown sender can never reach a classifier, because the
owner cannot name a contact they do not know.

`_fire` wraps the payload in `UNTRUSTED_OPEN` / `UNTRUSTED_CLOSE` markers, each
carrying a per-fire nonce so a sender can't type the close marker into their own
message and land the rest of their text outside the frame. It does this
because the wake dispatches as `sender="system"` and so takes the owner path in
`dispatch.handle` — a full turn, with external text as trusted-origin content.
The marker does not make injection impossible; nothing does. It is the cheapest
thing that helps, and it applies to every fire, not just groups.

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

## The account boundary — chief as its own system user (#286)

By default the installer gives chief its **own system account**, and its own
email-only Apple ID, so the owner texts it as an ordinary contact. Declining
falls back to the single-user install, which stays supported — but the account
is where several of the trust claims above actually get teeth.

What the boundary buys:

- **Chief's shell tool, self-edit and file access carry chief's authority, not
  the owner's.** Before, "chief made a mistake" and "the owner's home directory
  is gone" were the same event.
- **The owner cannot read chief's credentials without escalating.** The tree is
  owned by chief and shared with the group so the owner can still *edit the
  code*; `secrets/` is carved back out (`chmod -R go-rwx`) and is chief-only.
  The carve-out runs **after** the group sweep — reversed, it re-opens what it
  just closed (`install/account.py`).
- **The owner picks what chief may reach.** The wizard asks two questions —
  directories to read, directories to write — defaulting to **none**, applied
  as group permissions. The home directory root is never offered
  (`grant_reason`).

### What the boundary does not cover

**It is accident-scoped, not adversary-proof.** The tree is chief-owned and
group-writable, so chief can rewrite every file in it — including
`src/chief/install/*.py`, `.git/hooks/*` and `.git/config`. The `chief` launcher
and any `git` command the owner types inside that tree run **as the owner**, so
chief-authored code executes with the owner's uid and the owner's cached sudo
timestamp. `chief update` is the documented deploy path and it goes through
exactly that. "Its mistakes stop at its own account" is true; "an injected chief
cannot reach the owner" is not, and this document does not claim it. Closing
that would mean re-execing the launcher under `sudo -u chief`, which is not what
ships today.

The corollary: **treat the tree as chief's code, not yours.** Read a self-edit
diff before running anything in that directory, the same way you would a pull
request from a stranger.

- **Chief gets no privilege escalation. None, not deferred.** When it needs a
  system package it tells the owner what to run. Granting package installation
  would be root-equivalent, because package scripts run as root.
- **Chief keeps read access to the owner's message store**
  (`imessage.owner_db_path`). This is a considered trade, stated plainly rather
  than hidden: the owner runs monitors on their own conversations, and that
  access already exists today, so refusing it would forgo an improvement rather
  than close a new hole. It is read-only, and it is the one dataset the new
  boundary does not cover.

The account never changes who is `owner`. Trust is still identity-derived from
the adapter (§1): under `imessage.mode: dedicated` the owner is an ordinary
correspondent whose handle is in `owner_handles`, and chief's own handles in
`self_handles` are dropped from the owner's store so chief cannot mistake its
own reply for input.

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
10. **No unscoped classifier monitor ever reaches a model** — scope is checked
    before the classifier, and an unscoped row is disabled at boot.
11. **`secrets/` is chief-only, and the carve-out runs after the group sweep.**
    Reversed, the sweep re-opens it.
12. **Chief never escalates *on its own*.** No sudo rule, no broker, no approval
    flow — it reports what the owner should run. The boundary is accident-scoped,
    not adversary-proof: see "What the boundary does not cover".
13. **A non-interactive install never creates a system account.**

Tests for this layer: `tests/test_gate.py`, `test_approvals.py`,
`test_audit_and_bus.py`, `test_budget.py`, `test_instance_lock.py`,
`test_auth.py`, `test_dispatch.py`.
