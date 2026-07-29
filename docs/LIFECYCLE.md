# LIFECYCLE — how a message becomes a reply

The single most useful thing to hold in your head before changing anything in
core. Every inbound path converges on one callable, and every reply leaves
through the adapter named on the message that started it.

Symbols are named rather than line-pinned — this repo edits itself, so line
numbers rot. Grep the symbol.

## The one-line version

```
adapter (imessage | socket | web)  ─┐
monitor / cron wake ────────────────┼─→ Dispatcher.handle → Session.run_turn
                                    │        → agent.loop.run_turn → tool loop
                                    └────────────────← adapter.send(thread_key, text)
```

## 1. Boot wiring

`build_app` (`src/chief/app.py`) runs phases in a fixed order:
`build_persistence` → `build_gate` → `_build_agent_core` → `register_native_tools`
→ `CommandSet` → `build_mcp` → `build_adapters`.

The closure cycle is deliberate: `tools_factory` references `dispatcher`, which
is constructed *after* it — late binding resolves at call time. Don't "fix" it
by reordering. `MonitorService` and `CronService` both receive `dispatcher.handle`
as their `wake` callable.

`App.start()` (`daemon.py`) then starts socket → imessage → cron → web → MCP.

## 2. Inbound — three channels plus system wakes

All converge on `Dispatcher.handle` (`dispatch.py`).

### iMessage — `IMessageAdapter` (`adapters/imessage.py`)

1. `start()` loads the persisted rowid cursor. **Cursor 0 means first boot →
   jump to the max ROWID; history is never replayed.**
2. `_poll_loop()` ticks every `poll_seconds` (default 2.0), swallowing per-tick
   errors so one bad row can't kill the poller.
3. `poll_once()` walks each configured store (`Store`, `imessage_cursor.py`)
   and runs `POLL_QUERY` (`imessage_store.py`) against a **read-only** sqlite
   URI (`mode=ro`) in a thread, `LIMIT POLL_BATCH_LIMIT`.
4. Per row, **the cursor is saved BEFORE the turn runs**. This is the
   at-most-once invariant: a hard crash mid-turn drops that row rather than
   answering it twice. Graceful restarts drain instead (§6).
5. `_map()` → `Message` or `None`. Skips, in order: empty text; text starting
   with `BOT_PREFIX` (chief's own echo); `from_me and not in_self` (the owner's
   outbound copy to someone else).
6. `RecentDedup.is_duplicate()` on `(thread_key, sender, text)` within
   `DEDUP_WINDOW_NS` (5s, in-memory).
7. `resolve_approval()` is called **at poll stage, before enqueue** — see §5 for
   why that ordering is load-bearing.
8. `ThreadFifo.put()` (`imessage_fifo.py`) → a per-`thread_key` `asyncio.Queue`
   with a worker task spawned lazily. FIFO within a thread, parallel across.
9. That worker calls `dispatcher.handle(m, fire_restart=False)`, then
   `restart.fire_if_requested()` *after* the turn — safe, because the cursor is
   already durable.

Steps 3, 5 and 6 above describe `imessage.mode: self` — chief on the owner's
Apple ID. Under `mode: dedicated` (chief's own Apple ID, own user session) the
poll runs with an **empty** self-chat scope, so only `is_from_me = 0` rows
qualify; the `BOT_PREFIX` skip and `RecentDedup` are both bypassed, and
`send()` stamps no prefix. The scope is turned off, never repointed at the
owner's handle: that chat is chief's real conversation with the owner, so
scoping it would poll chief's own replies back as owner input.

Dedicated mode also polls a **second** store when `imessage.owner_db_path` is
set — the owner's own `chat.db`, so monitors the owner already relies on keep
working. Each store keeps its own cursor (rowids are per-store). One row class
is dropped from that store and only that store: rows whose sender is in
`imessage.self_handles`. Those are **chief's own replies seen from the owner's
side**, ordinary `is_from_me = 0` inbound rows with no prefix left to mark
them — delivering them would rebuild the echo loop by another route.

### Socket / CLI — `SocketAdapter` (`adapters/socket.py`), name `cli`

Unix socket at `config.socket_path`, newline-delimited JSON. In `{thread, text}`,
out `{type: delta|final, thread, text}`. `thread_key = f"cli:{thread}"` and
`sender="owner"` unconditionally — **a local socket connection is by definition
the owner**. The last connection to speak on a thread receives its replies. No
per-thread serialization at this layer; ordering comes only from `Session._lock`.

Client is `chief-cli` (`cli.py`) — a client only, so quitting it leaves the
daemon running.

### Web — `WebAdapter` (`web/adapter.py`) + `build_web_app` (`web/app.py`)

`POST /send` authenticates, builds a `Message(channel="web", sender="owner")`,
fires a task, returns 202 immediately. A web-origin turn's outbound is
**broadcast to every open SSE client** through the `ObserverHub` (`hub.py`), not
addressed per-connection — every browser sees every frame and filters
client-side on the `thread` field. A completed turn on **any other** channel
emits one coarse `tick` frame to the same hub (`Dispatcher._tick`), so the
cockpit can watch a thread it isn't tapped into. The `WebAdapter` is now only the
`web:` origin channel; the old EventBus mirror it used to run is gone.

### System wakes

`MonitorService._fire` and `CronService._fire` construct
`Message(sender="system", ...)` and call `dispatcher.handle` directly. No channel
ingress, no bus publish (§3).

## 3. The bus — `bus.py`

`Event` is a frozen dataclass (`type`, `channel`, `payload`). `EventBus.publish`
iterates handlers **sequentially and awaits each**, wrapping each in try/except:
a failing handler can't block the others, but a *slow* one can.

Only publisher: `Dispatcher._publish`, type `"message.inbound"`. Only in-tree
subscriber: `MonitorService._on_event`.

**Publish invariant:** strangers are published but not dispatched; owner messages
are published *and* dispatched; `system` senders are dispatched but **never
published** — that is what stops a monitor from waking itself. `MonitorService`
additionally early-returns on `sender == "owner"` to avoid double-waking.

## 4. Dispatch — `dispatch.py`

`Dispatcher.handle(message, fire_restart=True)`, in strict order:

1. `resolve_approval()` — if this answered a pending card, **return, no turn**.
2. Sender is neither owner nor system → `StrangerLog.log()` (metadata only,
   never content), publish, **return**. Strangers never run a turn.
3. `CommandSet.run()` — `str` result sends deterministically and returns;
   a `Message` result **rewrites the turn** (skill invocation); `None` means not
   a command.
4. Owner → `_publish`.
5. `_run_turn`.
6. `fire_restart and restart` → `fire_if_requested()`, after the reply is sent.
   iMessage passes `fire_restart=False` and fires in its own worker instead.

`_run_turn` resolves the adapter from `message.channel`, gets or creates the
session, wires `on_delta` → `adapter.send_delta`, and runs the turn. Error
handling splits deliberately: `ProviderError` surfaces `f"error: {exc}"` to the
owner because it is actionable (bad key, budget, model name); anything else
returns a generic message rather than leaking internals.

**Concurrency:** the dispatcher is *not* serial. Serialization comes from three
places — iMessage's per-thread FIFO worker, `Session._lock` per thread, and
`SessionManager._semaphore` globally (`max_concurrent_sessions`).

## 5. Session and the turn — `agent/session.py`, `agent/manager.py`

`SessionManager.get_or_create` resumes the transcript from the store, so history
survives restarts. Tools come from `tools_factory(thread_key, channel)`,
producing a per-session `GatedTools`. Before handing the history back it runs
`_repair_interrupted`: it finds the *last* assistant message carrying
`tool_calls` (by role, not by tail position — a crash mid-way through a
parallel call leaves the tail on a tool-role message instead) and appends a
synthetic error result for each of its call ids missing a matching tool-role
result, so the transcript stays valid (the provider requires every tool_call
answered) instead of reporting that call `pending` forever — see
`SUBSYSTEMS.md`'s web UI section for the transcript-view side of this.

`Session.run_turn` nests locks in an order that matters:

```python
async with self._lock:            # per-thread, ordered
    async with self._semaphore:   # global concurrency cap
        await self._gate.enter_turn()
```

**The per-thread lock is taken BEFORE the global semaphore**, so turns queued on
one busy thread can't starve every concurrency slot.

`Session._one_turn` then:

1. Checks `budget.status()`. `EXHAUSTED` with no `downgrade_model` →
   `refuse_over_budget` commits the user message and a refusal to the transcript
   and **never reaches the provider**. Otherwise the downgrade model is swapped in.
2. `_maybe_compact()` → `Compactor.compact`, replacing both in-memory history and
   the stored copy.
3. `_assemble_system(user_text, sender)`, then commits the user message to the
   store immediately (never lost to a mid-turn crash) and builds
   `transcript = [system, *messages, user]`, recording a baseline index.
4. Calls `agent.loop.run_turn`, which **mutates `transcript` in place** and, via
   the `on_commit` hook, persists each assistant turn and tool result to the
   store the instant it's produced (`Session._live_append`) — not batched at
   the end. This is what makes a tool call's `pending` state in the web
   transcript (`SUBSYSTEMS.md`) real: a thread read mid-turn sees the call
   committed before its result.
5. Slices new messages off the baseline, extends in-memory history (already on
   disk from step 4). If `run_turn` raises instead — a provider/network
   failure mid-turn — the same slice-and-extend still runs, in an `except`,
   before the exception propagates: whatever `_live_append` already
   committed stays in sync between the store and `self._messages` rather than
   orphaning the turn on disk with the in-memory copy never learning of it.
6. Runs `post_turn` hooks, then `settle_budget` (which may attach `result.notice`).

**The system prompt is never persisted.** It is assembled fresh each turn, so
prompt, soul, and package edits take effect on existing threads immediately.

`_assemble_system` composes: soul on top (legacy placement, not a `<hook>`
block), then the base prompt plus an origin note naming the channel, then
`pre_turn` hooks, plus `session_start` hooks only on the thread's first turn of
this process. Blocks are sorted by package name for deterministic placement.

Hook safety: every hook runs under `asyncio.wait_for`; failures are logged and
dropped. `render_block` escapes `<hook>` tags in contributed text and slugs the
source attribute — contributed text may be attacker-influenced.

## 6. The tool loop — `agent/loop.py`

`run_turn` loops up to `MAX_ITERATIONS`:

1. `_stream_once` iterates `provider.stream(...)`; each `TextDelta` is awaited
   straight out to the adapter. The terminal `Completion` is captured; a stream
   ending without one raises.
2. Appends the assistant message.
3. **No tool calls → return `TurnResult`.** The only success exit.
4. Per `ToolCall`: the key is `(name, canonical-json args)`. Seen more than
   `REPEAT_LIMIT` times *this turn* → refused with a course-correcting error
   string instead of running. Otherwise `tools.dispatch(call)`, then the
   `post_tool` hooks screen the result — annotate or veto — before step 5
   appends it. The repeat-limit refusal is not screened.
5. Appends the tool result.
6. Exhausting the iteration cap returns an explicit error string.

### Gate, then registry

`GatedTools.dispatch` (`gate.py`) decides before the registry runs. An unknown
tool name is recorded and passed through to the registry for a helpful error —
**a hallucinated name never raises an approval card**. See
[SECURITY.md](./SECURITY.md) for the decision table.

`ToolRegistry.dispatch` (`tools/registry.py`) **never raises**: `TypeError` becomes
a bad-arguments string, anything else becomes an error string. Tool handlers
follow the same rule — return error strings, don't raise.

Approval cards ride the session's own channel: the `ask` closure resolves
`dispatcher.adapter(ctx.channel).send`, so the card appears where the
conversation is — and also mirrors to any dashboard client tapped into that
thread via `dispatcher.tapped()`, answerable there through the same broker
(`POST /approve`; see [SECURITY.md](./SECURITY.md#approvals--approvalspy), #267).

## 7. Reply out

The origin channel rides on `Message.channel`; `Dispatcher.adapter()` looks it
up, and a `KeyError` there is by design a wiring bug rather than a silent drop.

- **Deltas** stream during the turn via `on_delta` → `adapter.send_delta`.
  `Adapter.send_delta` is a deliberate **no-op default** (`adapters/base.py`) —
  iMessage doesn't stream; socket and web do.
- **Final** is `adapter.send(thread_key, result.text)`, then optionally a second
  send for `result.notice`.
- iMessage prefixes `BOT_PREFIX` **only when the thread is an owner handle**,
  then passes handle and text to JXA as **argv, never spliced into the script**.
- Socket writes to the writer stored for that thread; with no live connection the
  frame is logged and dropped.

## Invariants worth not breaking

1. **At-most-once (iMessage):** cursor persisted at read, before the turn.
2. **Restart fires at the outermost boundary** — dispatcher for socket/web, the
   iMessage worker after the cursor is durable — never inside `Session.run_turn`
   where the reply is still unsent. `enter_turn` holds new turns while
   `fire_if_requested` waits on idle, bounded by `DRAIN_TIMEOUT_SECONDS`, then
   `os.execv`. See [ARCHITECTURE.md](./ARCHITECTURE.md#self-edit-the-restart-pipeline).
3. **Approval answers must be consumed before enqueue** on any channel with a
   serialized per-thread worker — otherwise the answer queues behind the very
   turn it would unblock. This is a fixed deadlock, not an accident.
4. **Ordering:** iMessage is FIFO per thread, parallel across threads. Socket and
   web have no adapter-level ordering. Bus delivery is serial and awaited.
5. **Self-DM posture:** chief runs on the owner's own Apple ID, so self-chat rows
   carry `is_from_me = 1` and the body lives **only in `attributedBody`** with
   `text` NULL — hence `decode_attributed_body`. A row qualifies when
   `is_from_me = 0` OR it is in the owner's self-chat, which is exactly what
   keeps the owner's other conversations out.
6. **Group chats** are the one case where `thread_key` (the chat identifier) and
   `sender` (the raw handle) differ. Groups take the stranger path on purpose:
   published for monitors, never answered. A group `sender` must **never** map to
   the owner — it would let anyone in the group present as the owner, and would
   reply to a thread the one-to-one send path can't address.
7. **Echo-loop seatbelt:** `owner_send_guard` blocks any `imsg`/`osascript` shell
   command referencing an owner handle. Handles are lowercased and `+`-stripped,
   since Apple-ID emails are case-insensitive.
8. **Web SSE is a broadcast, not a route.**
