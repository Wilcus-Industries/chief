# SUBSYSTEMS — web, monitors, classifiers, subagents

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
| `POST /send` | 202 accepted, fire-and-forget |
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
  `send`/`send_delta`, reaching only the client tapped into that thread.
- A client **tapped into** any other thread also gets that turn's
  `inbound`/`delta`/`tool`/`final` frames via `Dispatcher.tapped()`, which
  checks `hub.is_watched()` before calling `to_watchers` — cheap for a thread
  nobody is watching. Out-of-turn cards (see Approvals surface below) reach a
  tapped thread the same way. **Every** completed turn, on every channel, also
  emits one coarse `hub.tick()` frame (`{type, thread, channel, preview}`) so
  the cockpit can track a thread it isn't tapped into (sidebar reorder/unread/
  snippet) without mirroring its whole transcript.

A `tick` bumps the thread to the top of the sidebar, marks it unread, and shows
the reply preview as a snippet; a `delta`/`final` for an unknown thread
triggers a session reload.

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
name}`) — no args or result, so the row list's size tracks message count, not
tool-output size. System prompts and empty turns drop out. `find_tool_call` looks
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
| `instruction` | `{kind: classifier, classifier: "wake-judge", fire_label: "YES", instruction}` |
| `classifier` + `fire_label` | `{kind: classifier, classifier, fire_label}` |

Evaluation in `_matches`:

- `code` reads **exactly one** payload field (default `text`) and runs
  `re.search(pattern, value, re.I)`.
- `classifier` serializes the whole payload, prepends the instruction for the
  sugar form, and fires iff the returned label equals `fire_label`.
- Unknown kind logs a warning and returns `False`.

**`MATCHABLE_FIELDS` is `("text", "sender", "thread_key")`** and is validated at
create time. A field outside that set would match `""` forever and never fire
(issue #235). Note `text` is the bare message body and **never includes the
sender**, so matching a contact requires `field="sender"`.

Create-time validation also requires exactly one of the three forms, a
description, a context, `fire_label` with `classifier`, `field` only on the
pattern form, and that the named classifier **exists and declares that label**.

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

`classify(name, text)`:

1. Resolve the definition; unknown name raises `ClassifierError`.
2. Model precedence: `ClassifierDef.model` > `models.default_classifier` >
   `default_model`. The role map lets every classifier run on a cheap small model
   without touching the main agent model.
3. System prompt = body + an instruction to respond with exactly one label.
4. Two messages, `tools=[]` — a single flat completion, no tool loop.
5. **Retry up to 3 attempts**; exhausting them raises `ClassifierError`.

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

### The shipped classifier

`classifiers/wake-judge.md` — labels `[YES, NO]`, no model pin. Its prompt expects
the user message to hold an instruction followed by an event payload, which is
exactly the shape `_matches` constructs and the hardcoded target of the
`instruction` sugar.

## Subagents — `src/chief/subagents.py`

Structurally near-identical to classifiers — same registry and frontmatter shape —
but with a tool and a real tool loop. Uses plain `yaml.safe_load`; there is no
label problem here.

`AgentDef`: `name`, `description`, `system_prompt`, `tools | None` (None = every
tool minus spawn), `model | None`.

`spawn_agent(name, task)`:

1. Unknown name returns an error string enumerating the known agents.
2. Runs the standard `agent.loop.run_turn` with the agent's system prompt and the
   task, on `definition.model or default_model`.
3. `on_delta` is a **no-op** — subagent output does not stream to any channel;
   only the final text returns.
4. Records cost against the budget under `f"subagent:{name}"`.
5. Returns `result.text` as the tool result.

### Tool filtering and the recursion guard

`FilteredTools` wraps the shared dispatcher rather than building a new registry,
so subagents get the live toolset, filtered.

**`_allowed` hardcodes `spawn_agent → False` before consulting the allowlist**, so
a subagent can never spawn further subagents even if its frontmatter names it.

The filter applies in both directions — `specs()` hides disallowed tools from the
model, and `dispatch()` re-checks at call time and returns an error string.
Defense in depth against a hallucinated call.

**Subagents never fire hooks.** Only `Session._one_turn` does.
