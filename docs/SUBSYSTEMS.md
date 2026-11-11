# SUBSYSTEMS — web, monitors, classifiers, subagents, peers

The parts that sit beside the turn loop. For the loop itself see
[LIFECYCLE.md](./LIFECYCLE.md); for the gate and approvals see
[SECURITY.md](./SECURITY.md).

## The three-registry pattern

Three subsystems share one shape, and learning it once covers all of them:

| Directory | Registry class | Loader | Has a tool? | Validated by done-check? |
|---|---|---|---|---|
| `skills/` | `SkillLibrary` | `skills.py` | `load_skill` | no |
| `agents/` | `AgentRegistry` | `subagents.py` | `spawn_agent` | no |
| `classifiers/` | `ClassifierRegistry` | `classifiers.py` | **no** | **yes** |

Each is a directory of frontmatter-markdown files, each has a `*_dir` config key,
and each **rescans on every call** — no caching, so a self-edit applies without a
restart.

## Web UI — `src/chief/web/`

**Server-rendered shell only.** Two static HTML strings in `pages.py`; no template
engine. All content is filled client-side by `script.py`. The only server-side
substitution is `{error}` on the login page. CSS and JS live as Python string
constants served as separate assets so the HTML stays cacheable.

Routes, assembled in one `Starlette(routes=[...])` block in `app.py` — `/approve`
and `/events` are built by `web/live.py`, `/policy` by `web/policy_routes.py`,
both spliced in (#267, #265, keeps `app.py` under the file-length cap):

| Route | Purpose |
|---|---|
| `GET /` | login page or chat page |
| `POST /login` | sets cookie, 303 to `/` |
| `POST /send` | `{thread, text, interrupt?}` → 202 whose body says what became of it: `queued`, `injected` (into the running turn), `answered` (an approval card), `command` (slash command) |
| `GET /sessions` | JSON buffer list |
| `POST /delete` | buffer deletion |
| `GET /history` | transcript for one thread — tool calls collapsed to name + call id |
| `GET /history/tool` | one tool call's args + result, fetched lazily |
| `GET /commands` | `/command` palette |
| `POST /approve` | answer a pending approval card — `{thread, answer}`, 200/409 |
| `GET /events` | SSE stream |
| `GET /policy` | a thread's resolved stream policy + provenance (override vs channel default) |
| `POST /policy` | set (or, with `policy: null`, clear) the thread's override |
| `GET /monitors` | plaintext monitor list |
| `GET /app.css`, `GET /app.js` | static assets, unauthenticated |

Auth is covered in [SECURITY.md](./SECURITY.md) — the short version is that it
fails closed twice, and with no password **no HTTP listener is built at all**.

### Buffer switcher

Server side, `store.list_sessions()` returns `{thread, channel, model, count,
last}` per thread, newest-activity first, falling back to creation time for empty
threads.

Client side: buffers get an index number and a channel badge for non-web threads.
A `×` kill control appears **only** for web buffers that aren't `web:main`.
`Alt+0..9` jumps to buffer N. A buffer whose channel isn't `web` is attached
**read-only** — input disabled, banner shown — because typing into an iMessage
thread from the web console would send as the owner on a channel the console
doesn't own.

Deletion is guarded twice: the route rejects anything not prefixed `web:` and
rejects `web:main` outright (400), then `manager.delete()` returns `False` if the
thread is mid-turn, surfacing as **409**. `_wipe` holds the session lock across
the store write to close a delete/clear TOCTOU, and is the single guard shared by
the `session` tool, `/prune`, and the web console.

### Live stream

The fan-out is `ObserverHub` (`hub.py`), a core seam decoupled from origin
channels and the monitor bus: `listen(thread)` registers a per-client queue
(`None` = coarse-only), `drop()` unregisters, `broadcast()` pushes a frame to
every client, `to_watchers(thread, frame)` pushes only to clients watching that
exact thread. `App.stop()` calls `hub.close()`, which pushes a `closed`
sentinel that terminates the SSE generator; a `finally` always drops the queue
on disconnect. `/events?thread=` subscribes to the hub, not the adapter.

Two frame shapes reach the browser:

- A **web-origin** turn streams `delta`/`final` frames — `WebAdapter` (the
  origin channel for `web:` threads) calls `hub.to_watchers` from its
  `send`/`send_delta`, reaching only the client tapped into that thread. Its
  `inbound` (at turn start, or when an injected message is taken), `tool` and
  `thinking` frames come via `Dispatcher.tapped()`, as for any thread.
- A **slash command's answer** is a `command` frame (`{thread, command,
  text}`) via `tapped()` on every channel, never a `final` — so the client
  can't mistake it for a reply a running turn is streaming. Web sends it
  nowhere else; other channels still reply through their adapter.
- A client **tapped into** any other thread also gets that turn's
  `inbound`/`delta`/`tool`/`thinking`/`final` frames via `Dispatcher.tapped()`, which
  checks `hub.is_watched()` before calling `to_watchers` — cheap for a thread
  nobody is watching. Out-of-turn cards (see Approvals surface below) reach a
  tapped thread the same way. **Every** completed turn, on every channel, also
  emits one coarse `hub.tick()` frame (`{type, thread, channel, preview}`) so
  the cockpit can track a thread it isn't tapped into (sidebar reorder/unread/
  snippet) without mirroring its whole transcript.

A `tick` bumps the thread to the top of the sidebar, marks it unread, and shows
the reply preview as a snippet; a `tick` for an unknown thread triggers a
session reload.

### Approvals surface

Cards mirror to the dashboard (#267). `gate.approval_asker`'s `ask` closure
sends the question on the origin channel *and* calls `dispatcher.tapped()` with
an `approval` frame, so a client tapped into that thread renders it as a card
with yes/always/no buttons — not as ordinary chat text. A button `POST`s
`/approve` (`{thread, answer}`), which calls `ApprovalBroker.resolve` directly:
the identical broker a typed origin-channel reply resolves, so first answer
wins from either surface and the loser gets a no-op (409). Once answered, `ask`
emits an `approval_resolved` frame so the dashboard clears the card. A client
that taps into a thread *after* the card was raised still sees it — `/events`
replays `ApprovalBroker.pending_question(thread)` as an `approval` frame on
connect, so a blocked, watched thread never stalls silently.

### Serving

`WebServer` wraps `uvicorn.Server` and runs it as an `asyncio.Task` **inside the
daemon's own event loop** — no separate process. Lifecycle is owned by the
daemon. Both server fields are `| None` because the web UI is optional.

`view.py` maps stored wire messages to display rows: a text row (`{role: owner|
chief, text}`, relabeling `user`→`owner`/`assistant`→`chief`) and, for each tool
call an assistant message made, its own collapsed row (`{role: tool, call_id,
name}`) — no args or result, so tool output never inflates the row list —
preceded by a `{role: thinking, text}` row when the message stored reasoning
(sent inline, so a reasoning model's rows do grow with its thinking). System
prompts and empty turns drop out.

Client side, a run of tool calls and thinking renders as one collapsed
activity group labelled with the latest call's name. The transcript re-renders
from `/history` only on a buffer switch, an SSE reconnect, or a command answer
while idle (`/clear`, `/compact` rewrite what is stored) — never per frame. A
slash command and its answer are never stored, so the client keeps them as
client-only rows marked "not saved", anchored by stored-row count and
re-placed after each re-render (dropped once the transcript shrinks past
them). `find_tool_call` looks
up one call's args + result on demand for `GET /history/tool`; a result that
hasn't landed yet (call committed, its tool-role message not — mid-turn, or a
crash `SessionManager` hasn't repaired yet) reports `pending`, and a call id
absent from the transcript altogether reports `pending` while the thread is
mid-turn or `compacted` once it isn't (folded away by compaction).

## Monitors — `src/chief/monitors/`

A monitor is a persisted, agent-created event subscription: watch a channel,
evaluate a predicate per event, wake a thread on a match. The wake target is
**decoupled** from the watch target — a monitor created in one thread can watch a
different channel.

Stored as `MonitorRow`: `id`, `description`, `watch_channel`, `wake_channel`,
`wake_thread`, `predicate` (JSON), `enabled`.

### Three agent-facing predicate forms, two internal kinds

`instruction` is **sugar** that lowers to a `classifier` predicate. Internally
only `code` and `classifier` exist.

| Agent form | Lowers to |
|---|---|
| `pattern` | `{kind: code, field, pattern}` |
| `instruction` | `{kind: classifier, ..., fire_label: "YES", instruction, scope}` |
| `classifier` + `fire_label` | `{kind: classifier, classifier, fire_label, scope}` |

Both classifier forms carry a `scope` — `{sender}` or `{thread_key}` — checked
in `_on_event` **before** `_matches`, so an out-of-scope event never reaches the
model. The pattern form takes none. That is a prompt-injection boundary, not an
optimization: see `docs/SECURITY.md`.

Evaluation in `MonitorGate.evaluate` (`monitors/gate.py`), once per event over
every in-scope monitor:

- `code` reads **exactly one** payload field (default `text`) and runs
  `re.search(pattern, value, re.I)` — locally, no request.
- If any `code` matched or any `classifier` is in scope, the payload goes to the
  injection screen in **one** call carrying a `judge_<id>` Choice per
  classifier monitor (its definition prompt plus the instruction). When
  TypeSafe did not answer the judges, each falls back to its own model turn
  (payload with the instruction prepended for the sugar form). A monitor fires
  iff its label equals `fire_label`; one judge raising skips only that monitor.
- Unknown kind never fires.
- A flagged or unscreenable event fires **withheld** (`_fire_withheld`: sender
  and thread only). See SECURITY.md "Injection screen".

**`MATCHABLE_FIELDS` is `("text", "sender", "thread_key")`** and is validated at
create time. A field outside that set would match `""` forever and never fire
(issue #235). Note `text` is the bare message body and **never includes the
sender**, so matching a contact requires `field="sender"`.

Create-time validation also requires exactly one of the three forms, a
description, a context, `fire_label` with `classifier`, `field` only on the
pattern form, a scope on the classifier forms only, and that the named
classifier **exists and declares that label**.

### There is no scheduling

Monitors are purely event-driven. `MonitorService` subscribes to the bus, so
evaluation happens synchronously inside `EventBus.publish`. Time-based waking is
a separate subsystem: `src/chief/cron/`.

Each event re-reads `list_enabled()` from the DB, so monitor changes take effect
immediately without a restart.

### The self-wake loop is cut in two places

1. `_on_event` returns immediately when `sender == "owner"` — owner messages
   already dispatch a turn on every channel, so firing on them would double-wake.
2. `_fire` wakes with **`sender="system"`**, which the dispatcher refuses to
   re-publish to the bus.

Strangers — published but never dispatched — are the intended trigger. That is
the complement of the dispatcher's stranger path, and it is what lets notify
policy be agent policy rather than hardcoded code.

Error isolation is two-layer: a classifier raising is caught inside the per-monitor
loop so it can't abort siblings, and the bus wraps each handler too.

### Tool surface

One `monitor` tool with an `action` enum of `create`/`list`/`delete`,
`wants_context=True`. Defaults come from context: `watch_channel or
context.channel`, `wake_channel=context.channel`,
`wake_thread=context.thread_key`. All failures return `"error: ..."` strings
rather than raising — this file is the reference pattern for new tools.

## Classifiers — `src/chief/classifiers.py` + `classifiers/`

An internal categorical-label operation over the model seam. **Internal and
self-edit only — there is no agent-facing tool.** Core services call one by name.
Monitors are the canonical consumer.

`ClassifierDef` is a frozen dataclass: `name`, `description`, `labels`, `prompt`,
`model | None`.

### The YES/NO YAML trap

`_LabelSafeLoader` strips the bool implicit resolver and re-adds a narrowed one
matching only `true/false` spellings. Without it, YAML 1.1 coerces unquoted
`labels: [YES, NO]` into Python booleans and destroys the label text. Don't
replace it with a plain `SafeLoader`.

### Invocation

`classify(name, text, *, model=None, typesafe=True)`:

1. Resolve the definition; unknown name raises `ClassifierError`.
2. **TypeSafe first** when `typesafe_api_key` is set (and `typesafe=True`): one
   Choice question — prompt as instructions, labels as the options, text as
   state. A typed pick, no parse. Any `TypeSafeError` (including an undeclared
   pick) logs a warning and falls through to the model turn below.
3. Model precedence: the `model` argument (the screen passes
   `models.screen_fallback`) > `ClassifierDef.model` > `models.default_classifier` >
   `default_model`. The role map lets every classifier run on a cheap small model
   without touching the main agent model.
4. System prompt = body + an instruction to respond with exactly one label.
5. Two messages, `tools=[]` — a single flat completion, no tool loop.
6. **Retry up to 3 attempts**; exhausting them raises `ClassifierError`.

`_match_label` does two passes, both case- and whitespace-tolerant: exact
equality, then a **prefix** match over labels sorted longest-first so a short
label can't shadow a longer one that starts with it. `None` drives the retry.

### validate() is part of the done-check

`validate(root)` returns a list of well-formedness problems, skipping
`README.md`. **The self-edit done-check calls it, so a bogus classifier write is
rolled back rather than merged.** Checks: leading `---`, closing `---`, parseable
YAML, mapping frontmatter, non-empty `name` / `description`, `labels` a non-empty
list of non-empty strings, non-empty body. The first three early-return; the rest
accumulate.

### The shipped classifiers

`classifiers/wake-judge.md` — labels `[YES, NO]`, no model pin. Its prompt expects
an instruction plus an event payload, the shape `MonitorGate` builds, and is the
hardcoded target of the `instruction` sugar.

`classifiers/injection-screen.md` — labels `[INJECTION, CLEAN]`. Its body is the
TypeSafe Noul instruction *and* the fallback model's prompt, so the two screens
ask the same question. Deleting it makes every screen "unavailable" (monitor
wakes withheld, tool results bannered) — loud, not silent.

## Subagents — `src/chief/subagents.py`, `src/chief/agent/subagent_*.py`

Definitions are structurally near-identical to classifiers — same registry and
frontmatter shape — but with a tool and a real tool loop. `AgentDef`: `name`,
`description`, `system_prompt`, `tools | None` (None = every tool minus spawn),
`model | None`.

`spawn_agent` (PRD #308) folds `action=spawn|list|cancel`:

- **spawn** (`name`, `task`, `wait`) starts a `SubagentRun` in the
  `SubagentPool` and returns `spawned sub:<parent>:<hex> (<name>)` at once. Its
  messages are `PREAMBLE` + the definition prompt, then the task. Unknown names
  return an error listing the known agents. `wait=true` blocks until the result —
  or returns early with `subagent X asks: … — still running, reply with
  send_msg(to=X)` on the run's first question, after which it is async.
- **list** / **cancel** are scoped to the caller's own runs; a cancel waits for
  the run to wind down and the parent is told `cancelled by parent`. The owner's
  `/tasks` and `/cancel <id>` cover every run (`cancelled by owner`).

Lifecycle (`agent/subagent_drive.py` `drive`): each turn is the standard
`agent.loop.run_turn` on `definition.model or default_model`, with a no-op
`on_delta`, the core `post_tool` screen, the run's inbox as the loop's `drain`,
and `stop` once `finish` was called. Cost is recorded under `subagent:<name>`
per model call (`on_usage`), so a cancelled or failed turn's spend still counts.
Before each turn the budget is checked: once exhausted the run switches to the
`downgrade` model role, or stops (`stopped: budget exhausted`) without one. A
kill or cancel notice's "last progress" is the run's latest assistant text, read
off the live transcript, so a run killed mid-turn reports where it got to.
A run that never messaged its parent auto-finishes with its last text; once it
has, a turn ending means idle — it waits for a message, then continues; it ends
on `finish(result)` or `subagents.idle_timeout_s` (kill notice with task and
last progress). Every ending reaches the parent through
`SubagentPool._result` → the router's inject-or-wake, as a message headed
`[message m-… from subagent <id> (<name>)]`. A turn holds one of
`subagents.max_concurrent` slots only while it runs, never while idle.

Orphans: `SessionManager.set_on_wipe` → `pool.cancel_children`, so any clear or
delete of the parent (session tool, `/clear`, `/prune`, cockpit) silently
cancels its runs (audited `subagent_cancelled`). Restart: the pool's
pre-restart hook writes `data/subagent_notices.json`; the fresh boot
(`entrypoint.deliver_subagent_notices`) tells each parent `killed by restart`
with the task and progress. Runs are never resumed.

### Tools, filtering, the recursion guard

A run's dispatcher is `RunTools(FilteredTools(tools_factory(parent, channel,
run_id), allow, subagent=True))`: the parent's **gated** tools (cards on the
parent's thread, audit `subagent` field, no announcements), filtered, plus
`finish`, which `RunTools` intercepts before the gate.

**`_allowed` refuses `SUBAGENT_NEVER` (`spawn_agent`, `restart`, `revert_edits`)
before consulting the allowlist**, so a subagent can never spawn further subagents,
nor restart or roll back the daemon, even if its frontmatter names them. A restart
fires only at a session turn's boundary; a detached run calling it would close turn
admission with nothing left to exec, wedging the daemon.
For a subagent `send_msg` is always allowed, or an allowlisted agent could never
reach its parent. The filter applies in both directions — `specs()` hides
disallowed tools from the model, and `dispatch()` re-checks at call time and
returns an error string.

**Subagents never fire hooks.** Only `Session._one_turn` does.

## Peers — `src/chief/peers/`

`send_msg(to, text, reply_to?)` (`peers/tools.py`) → `PeerRouter`
(`peers/router.py`). The router checks scope (no self-sends, a known
`thread_key`, only your own `sub:` runs, a subagent only to its parent), counts
the pair in `PairCounter` (`peers/loopguard.py`; past
`peers.loop_warn_count` in `peers.loop_window_s`, both sides get the warning
text — the sender in its tool result, the receiver under the message), audits
`peer_message`, and delivers: `Session.deliver` into a busy receiver's inbox
(drained after its next tool batch; a leftover after a text-only answer starts a
follow-up turn from the dispatcher), else a tracked background
`dispatcher.handle(Message(channel, "system", to, text))`. Never awaited inline —
that would stall the sender, or deadlock on a self-thread. The envelope
(`peers/envelope.py`) renders `[message m-<8 hex> from session <key> re m-…]`;
keep that header stable, core skills key on it.

Reaching the owner from a thread that isn't theirs is a forward, not a
separate tool: the `messaging` skill sends `send_msg` to the owner's iMessage
thread (else `web:main`), which tells them in its own transcript and routes
their answer back. `session action=list` tags owner-audience threads
`(owner)` (`policy.owner_audience`) so a session can find that target.
