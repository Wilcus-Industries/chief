# chief — Design

A personal AI agent, self-hosted on a VPS. DM it from Telegram or Discord and it does
things for you — and for other people, like a real assistant screens for its boss.
Backed by your Claude Max subscription via the Claude Agent SDK.

Status: **design complete (v1)** — ready to build; open items are technical verifications.

## Goals

- Reach the agent by DM from **Telegram** and **Discord** (Telegram first).
- **Two tiers of caller:** one privileged **owner** per platform = you; everyone else is
  a **guest**, explicitly *not you*, and gets a restricted "assistant" experience.
- Agent acts via MCP servers: **Google Drive, Gmail, Google Calendar** (owner only).
- Plus **web search/fetch**, a **file workspace**, and **shell/code** on the VPS under a
  strict command policy.
- Use the **Claude Agent SDK** (Python) so it runs on **Claude Max**, not API billing.
- Ship as one **docker compose** stack, deployable to a VPS.

## Design completeness — areas to settle

Tracking the full surface so "done designing" is well-defined. ✅ settled · 🚧 in progress · ⬜ todo.

- ✅ Identity & access (owner/guest tiers)
- ✅ Session memory (task = thread/topic)
- ✅ Guest scope + booking approval
- ✅ Command-policy tiers (values of the lists still TBD)
- ✅ Task execution engine — hybrid, bounded-parallel, milestones, live steering
- ✅ Agent core internals — tier isolation, tool exposure, model, Soul.md
- ✅ Approval flow — unified gate, default-ask, self-curating (deterministic) buttons
- ✅ Long-term memory & learning — markdown + wikilinks, namespaced, distill-on-staleness
- ✅ Security model — trust levels, notify-on-first-contact, rate limits, shell sandbox
- ✅ Data model & persistence — sqlite + md + JSONL, notify-on-restart, 90-day transcripts
- ✅ Config & secrets — Docker secrets + config.yaml/.env via pydantic-settings
- ✅ docker topology — outbound-only, long-poll, core/sandbox/mcp containers, CI/CD
- ✅ Ops — JSON logs, uptime heartbeat, backup, usage budgeting (own-share cap)
- ✅ Proactivity, scheduling & skills — reactive+reminders, monitors, skills, quiet hours
- ✅ Revisits — media, booking, onboarding, group chats, tools, model/skills/blocklist/i18n
- ✅ Edge cases & build-readiness — routing, guest-wait, auth expiry, casual chat
- ✅ Build artifacts — repo structure, message lifecycle, phased milestones
- ✅ Technical verification — auth, gate hook, usage telemetry, session resume confirmed
- ✅ Billing-driven forks — ask-then-overflow; owner credit pays for all

**Design status: complete + technically de-risked.** All product decisions made; the three
build-gating unknowns (auth, gate, usage) are verified. Remaining "Still to verify" items
(Google MCP, TG topics API, websearch-on-credit, STT) are routine build-time spikes.

## Decisions so far

| Topic | Decision |
|---|---|
| Access model | One owner per platform; all others are guests (receptionist mode) |
| Platforms | Adapter abstraction; **Telegram first**, Discord next |
| Sessions | Task = thread/topic (TG forum topics; Discord threads); guests flat (DM/mention) |
| Owner capabilities | Google MCPs, web search/fetch, file workspace, scoped shell |
| Guest capabilities | Take a message, check availability, request a booking (only) |
| Guest bookings | Require owner approval before landing on the calendar |
| Email | Gmail is a **tool only** for now — not a chat channel |
| Permission gate | One gate, **default-ask on effect**: NEVER refuse · read-only allow · APPROVED allow · else ASK |
| Approval routing | In task topic for owner work; **Front Desk** topic for guest-originated |
| Approval buttons | Approve/Deny once + Always-allow/deny (self-curating allowlist) |
| Models | Owner: Sonnet default, Opus on demand. Guests: Sonnet, never Opus |
| Personality | `Soul.md` — chief-editable, git-tracked, loaded into every prompt |
| Long-term memory | Markdown + `[[wikilinks]]` (graph-lite), namespaced; `MEMORY.md` index + on-demand fact files; auto-save+notify; no embeddings; mem0 swap deferred behind an interface |
| Memory mechanics | Distill on 10-min staleness; overwrite stale facts; TTL-flag time-sensitive; provenance per fact; keep full transcripts |
| Tier isolation | Guest sessions built with *only* guest tools (not prompt-instructed) |
| Guest admission | Notify-on-first-contact (owner allows new senders; known senders skip) |
| Guest rate limits | Per-guest cap + global guest budget (protect Max limits) |
| Shell sandbox | Dedicated locked-down worker container; no secrets, restricted FS/network |
| Audit | Log every tool call, approval decision, memory write |
| Persistence | One sqlite (tasks/contacts/approvals/policy/limits) + md memory + JSONL transcripts/audit |
| Restart | Interrupted tasks: notify + ask before resuming. Approvals survive restart |
| Transcripts | Keep, auto-prune after 90 days; distilled memory persists |
| Transport / ingress | Telegram long-polling; outbound-only, no public HTTP ingress |
| MCP servers | Separate containers (HTTP/SSE), one per service |
| Deploy | CI/CD: GH Actions → GHCR → SSH `compose pull && up` |
| Logging / uptime | JSON to stdout; external dead-man's-switch heartbeat |
| Backup | VPS auto-backups + memory git repo pushed to private remote |
| Auth | `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` (Docker secret); never set `ANTHROPIC_API_KEY`; regen yearly |
| Gate mechanism | `PreToolUse` hook (every tool) + `canUseTool` (ask→approval) — verified |
| Usage budget | Track month-to-date `total_cost_usd` vs monthly Agent SDK credit ($100/$200); warn 75/90%; on cap ask owner. (Post-June-15: separate from interactive limits) |
| Credit overflow | Hard pause at exhaustion; overflow to paid API rates only if owner approves |
| Guest billing | Owner's subscription credit pays for all (no separate guest key in v1); ToS grey-area noted |
| Proactivity | Reactive + reminders only; no unsolicited nudges |
| Scheduler | Reminders + recurring + monitors (predicate→action); chief self-manages; gate still applies |
| Skills | Packaged workflows (à la Claude Code); `setup-morning-brief` interviews owner |
| Quiet hours | Configurable; defer non-urgent, urgent breaks through |
| Media | In: images (vision) + PDFs/docs; voice deferred. Out: smart split + file attachments |
| Group chats | Mention-activated; tier by sender ID; lightweight flat sessions |
| Onboarding | Agent-followed `BOOTSTRAP.md`; auth scripts bundled with MCP servers |
| Google scope (v1) | Calendar R/W, Gmail R/W, Drive R/W |
| Web (v1) | Search + fetch (GET un-gated, POST gated) |
| Timezone | Single configured owner TZ |
| Booking | Negotiate within free/busy + memory prefs (hours/buffer/cap); video link only if asked; final owner approval; confirm guest |
| Sched prefs | Live in **memory** (natural language), not config; behavioral prefs → memory, infra → config.yaml |
| Away status | A time-sensitive memory fact ("on vacation til Mon"), not a mode |
| Guest times | Always owner TZ, timezone stated explicitly |
| Opus escalation | Always owner-approved: command pre-approves, auto-detect asks |
| Skill authoring | chief drafts, owner approves via review/merge; never self-deployed |
| Acting-as identity | Transparent — assistant signature on outgoing email/messages |
| Encryption at rest | Secrets encrypted (age); transcripts/memory via host perms + backups |
| Memory mgmt | `/memory`, `/forget` + direct file edit; auto-notify on save |
| Blocklist | Owner block/mute; chief self-blocks only on clear abuse (notifies owner) |
| Language | Match the sender |
| Testing | Mocked unit+integration + live sandbox (TG test bot, Google test acct) |
| Task close | Auto-archive on ~1-hr idle; reopens on next message |
| Failures | Auto-retry transient w/ backoff, then report; keep recoverable |
| Discord owner | Private server, channel threads = tasks (mirrors Telegram) |
| Addressing | Persona-driven from `Soul.md`/`User.md`; "<Owner>'s assistant" to guests |
| Notif routing | Task pings in-thread; standalone proactive → configurable primary platform |
| Guest waiting | "I'll get back to you" + async DM follow-up; no tentative holds |
| Auth expiry | Detect, pause affected work, ping owner with exact re-auth step |
| General topic | Casual chat; spawn a task topic only when work warrants it |

## Identity & access

- Config holds one owner ID per platform (`OWNER_TELEGRAM_ID`, `OWNER_DISCORD_ID`, …).
  Match = owner; anything else = guest. No fuzzy matching, no "maybe me."
- **Owner** gets the full toolset (their Google data, file workspace, shell-by-policy).
- **Guest** gets a tight **receptionist** persona. Allowed, and nothing more:
  1. **Leave a message** for the owner (relayed/forwarded).
  2. **Check availability** — free/busy only, never event details.
  3. **Request a booking** — proposes an event; **owner must approve** before it lands.
- Any guest request outside those three → polite decline ("I'm <owner>'s assistant; I
  can take a message or help find a time"). Guests never see the owner's private data,
  no web Q&A, no shell, no file workspace.
- **Away status is a (time-sensitive) memory fact, not a mode.** Tell chief "I'm on
  vacation til Monday" → it stores that with a TTL and tells guests accordingly (and pauses
  non-urgent nudges) until it expires. No special vacation toggle.

## Memory & sessions — task-based

Direction: **a conversation is a task.** Each task = one agent session, possibly several
in flight, chief reports progress per task. The adapter hands core a stable `thread_key`;
core maps `thread_key → task/session`.

- **Telegram (owner) — forum topics.** chief lives in a private supergroup (you + bot)
  with Topics on; **one topic = one task/session**. Bot creates topics via
  `createForumTopic` (needs `can_manage_topics`); inbound/outbound carry
  `message_thread_id`. `thread_key = (chat_id, message_thread_id)`. The `General` topic
  is the **casual inbox**: chief chats there for quick back-and-forth and **only spawns a
  dedicated task topic when a request clearly warrants tracked work** (avoids topic clutter).
- **Discord (owner)** — a **private server**; a channel's **threads = tasks**, mirroring the
  Telegram supergroup/topics model for a consistent mental model. Same `thread_key` idea.
- **Email** (deferred) — mail thread (`In-Reply-To`/`References`) → task, if added later.

**Guests don't get topics.** They reach chief by 1:1 DM or `@mention` in a shared group —
flat sessions fitting their receptionist scope. The adapter spans **three surfaces on one
bot token**: owner-in-supergroup (topic-routed tasks), guest-in-DM, and group-mention; it
tags tier by **sender ID**, not chat type. See "Channels, media & onboarding".

A task has a small lifecycle (full states in "Task execution engine"). Task/session
metadata lives in sqlite; **auth tokens do not** — they're Docker secrets (encrypted),
outside agent-reachable paths (see Config & secrets).

## Task execution engine

What happens between "you send a task" and "it's done." **Decided:** hybrid execution,
bounded parallelism, milestone progress, live steering.

- **Hybrid inline/background.** Every task runs as a live background session from the
  start. The adapter waits a short grace window (~5–8s) for the first result: if the task
  finishes fast → reply **inline** (feels synchronous); if not → post a "working…" ack and
  let it keep running, posting milestones. One mechanism, both feels.
- **Lifecycle:** `running → waiting(input|approval) → done | failed | cancelled`. Plus an
  idle **open** state (session alive, awaiting your next message). Persisted, so chief
  survives a restart mid-task (resume the SDK session by id).
- **Bounded parallel.** A live task session may sit *open/idle* without cost; a
  **semaphore (~3)** bounds only sessions actively *generating a turn*, protecting Max
  rate limits. Multitask freely; turns queue when the cap is hit.
- **Milestone progress.** chief posts on meaningful steps (tool-use start/end, key
  decisions), throttled — a readable work log in the topic. Sourced from SDK stream/hook
  events mapped to short lines, not raw token streaming.
- **Live steering.** Each task is a persistent SDK **streaming-input** session; a new
  message in its topic is pushed into the running session (with `interrupt()` for "stop,
  do X instead"). Ordering/interrupt edge cases are a known cost — handle explicitly.
- **Controls:** `/cancel` (or button) stops a task; `/tasks` lists in-flight with status.
- **Auto-archive.** After ~1 hr idle, chief marks the task done and archives its
  topic/thread; a later message **reopens it** with context intact (resume the SDK session
  by id). Keeps the list tidy. (Distinct from the ~10-min distill-on-staleness trigger,
  which only writes memory and leaves the task open — see Memory & learning.)
- **Failures:** transient tool/API errors **auto-retry with backoff**, then report clearly
  in-thread and leave the task recoverable (never silently swallow).

## Agent core internals (draft)

Wraps the Claude Agent SDK. One **session per task**, kept open in streaming-input mode
(see task engine). Key design choices:

- **Tier isolation by construction, not instruction.** Owner and guest sessions are built
  from *different tool sets*, not the same agent with a "don't do X" prompt. A guest
  session only ever has the three guest tools wired in (leave-message, check-availability,
  request-booking) — owner Google/shell/web tools are physically absent from it. Defense
  against prompt injection / jailbreaks: there's nothing to escalate *to*.
- **Tool exposure (gate mechanism verified — see "Verified"):** the permission gate is a
  **`PreToolUse` hook** (fires on every built-in + MCP tool call, returns allow/deny/ask);
  **`canUseTool`** drives the "ask" → approval round-trip.
  - **Shell** → SDK built-in Bash tool, gated by the hook against the command policy
    (NEVER/APPROVED/else-ask). Owner-only; executes in the sandbox container.
  - **File workspace** → SDK built-in Read/Write/Edit, confined to a workspace dir set as
    the session cwd. Owner-only.
  - **Web search/fetch** → SDK built-in (or a web MCP). Owner-only.
  - **Google (Calendar/Gmail/Drive)** → MCP server(s). Owner-only.
  - **Guest tools** → small custom MCP/in-process tools, the only ones in a guest session.
- **System prompt** = `Soul.md` (chief's identity/voice, see below) + tier framing (owner:
  capable operator; guest: polite receptionist boundary) + per-task framing. Composed per
  session and reloaded when `Soul.md` changes.
- **`Soul.md` — self-authored personality.** chief's voice/identity lives in a `Soul.md`
  file he can **read and edit himself** (owner can too). Lives on a persisted volume so
  edits survive restarts, and is **git-tracked so any change is reversible** (chief
  shouldn't be able to lobotomize himself irrecoverably). Loaded into every system prompt.
  **Addressing is persona-driven:** how chief refers to itself and to the owner comes from
  `Soul.md` + `User.md` (editable/evolving); to guests it's "<Owner>'s assistant".
- **Context management.** Long tasks risk filling the window → rely on SDK compaction if
  available; otherwise summarize-and-resume. Persist `session_id` to resume after restart.
- **Model choice.** Owner: **Sonnet 4.6 default, Opus 4.8 only with owner approval** —
  Opus burns the Max budget faster, so it never runs unsanctioned. A command (`/opus`,
  "think hard") **pre-approves**; auto-detect of a complex task **asks** first. Guests:
  **always Sonnet**, never Opus — requests are trivial but chief represents the owner, so
  phrasing quality matters.

## Long-term memory & learning

chief should get better at being *your* assistant over time. **Decided:** markdown files
with `[[wikilinks]]` for graph-lite relations; auto-save + notify; index-now /
retrieve-as-needed; no embeddings, no extra infra. Behind a memory interface so a
semantic+graph backend (mem0) is a later swap, not a rewrite.

**Layout** (git-tracked memory dir on a persisted volume — convention shared with OpenClaw
+ this environment), namespaced by subject:

- **`Soul.md`** — who chief *is* (identity/voice). chief-editable, git-tracked.
- **`User.md`** — the owner's profile: name, standing preferences, baseline. Owner-authored.
- **`MEMORY.md`** — the **index**: concise pointers to every fact/topic file. Loaded into
  every session (cap ~200 lines; spill detail to topic files).
- **`facts/owner/…`**, **`facts/contacts/<id>/…`** — one fact (or small cluster) per file,
  with frontmatter (`trust`, `expires`, provenance). Loaded **on demand** by the LLM when
  the index says a file is relevant. `[[wikilinks]]` connect them (entities = files,
  relations = links) — relational traversal without a graph DB.

**Recall:** the LLM reads `MEMORY.md` each session and opens linked fact files as the task
needs them (+ grep as a cheap fallback). No vector search until/unless scale demands it.

**Decided in discussion (mechanics, substrate-independent):**

- **Namespaced.** Memory is partitioned by subject: owner facts vs per-contact facts.
  Guest-stated facts are fenced into that contact's namespace and tagged low-trust.
- **Distill on staleness.** Full transcripts are kept (SDK behavior). When a conversation
  goes idle ~10 min, a **reflection step** (subagent/hook) reads it and writes distilled
  facts to memory. Memory = distilled facts; transcripts = the raw record. This is
  **separate from task auto-archive** (~1-hr idle, closes the topic): distill only writes
  memory and the task stays open.
- **Overwrite, don't accumulate.** A changed fact ("afternoons now") **overwrites** the old
  one rather than piling up contradictions.
- **Flag time-sensitive facts.** Any ephemeral fact ("out sick this week") is marked with
  an expiry/TTL so it ages out instead of lingering as truth.
- **Provenance.** Each fact records origin (owner-stated / inferred / guest-stated).
- **Auto-save + notify.** chief saves on its own and tells you what it saved; you can
  prune/correct. Explicit "remember that I…" also works.

**Hard safety rule — learning ≠ permission.** A learned memory can *suggest* a gate change
("you've approved this 3× — always allow?") but **can never enact one**. Permission lists
change only by explicit owner button-tap (deterministic path, enforced in code — see the
"enforce in code, not prompt" lesson). Poisoned content can write a memory; a memory can't
grant capability.

**Memory interface** (`recall`, `write`, `overwrite`, `expire`, namespaced) wraps the
markdown backend, so mem0/graph is a drop-in swap later if scale ever demands it.

**Owner management:** in-chat commands (`/memory` to list, `/forget X` to remove) **and**
direct hand-editing of the git-tracked markdown files. chief still auto-notifies on save.

### Research notes (memory landscape)

- **OpenClaw** — `SOUL.md` (persona) + `MEMORY.md` + `USER.md`/`IDENTITY.md` in a
  git-backed workspace; sessions as JSONL; **5-level trust** (owner→agent→allowlisted→
  stranger→untrusted-input). Lesson: compaction can drop safety rules — never store
  security constraints in chat/memory; pin them in code.
- **Claude Code** — `MEMORY.md` index (first 200 lines/25 KB loaded each session) + topic
  files loaded on demand; auto-memory decides what's worth keeping; plain editable md.
  Lesson: *memory/instructions are context, not enforcement — block via hooks.*
- **Memory MCP servers** — official = knowledge graph (entities/relations/observations,
  JSONL, 9 tools); mem0 = semantic+graph. Strong for relational contact queries; cost is
  manual graph upkeep + opacity. **Deferred** as a future upgrade for chief.

## Permission gate (default-ask)

One gate classifies **every** tool call, not just shell. Posture: **default-ask on
effect** — you curate what's auto-allowed; everything effectful else needs a tap (not a
silent refusal — unlisted effectful actions prompt, they don't fail).

Decision order for a call:

1. **NEVER list** → hard refuse, no prompt, no override (e.g. `rm -rf /`, disk wipes,
   curl-pipe-sh, reading secret/credential paths).
2. **Read-only / safe** → **allow**, no prompt. Reading files, web search/fetch (GET),
   calendar free/busy, listing, and **writes inside the scratch workspace**. Low blast radius.
3. **APPROVED allowlist** → **allow**, no prompt. Specific effectful actions you've blessed
   (e.g. `git status`, a named script, email to a known contact).
4. **Everything else effectful** → **ASK** (the approval flow). Shell not on the allowlist,
   send-email, deletes/overwrites outside the workspace, web POST/forms, calendar/Drive writes.

Only the **owner** triggers effectful tools at all; guest sessions don't have them wired in.

**Self-curation is the hard part (flagged).** "Always allow" must add a *precise, safe*
allowlist entry — argument-aware and metacharacter-free. Approving `git status` must not
bless `git status; rm -rf ~`. Plan: parse the command (no shell string), match on
`(binary, arg-shape)`, reject any entry containing shell metacharacters/pipes/redirects,
and never allow argument wildcards on destructive binaries. Same idea for non-shell tools
(allowlist = tool + constrained arg pattern, e.g. "email to alice@x.com").

## Approval flow

The round-trip that the gate's **ASK** verdict invokes (see Permission gate). Same
mechanism serves shell, sensitive owner actions, and guest-originated requests.

**State machine:** `requested → notified → approved | denied | timed_out→deny | cancelled`.
The triggering tool call blocks on a future; the task sits in `waiting(approval)` (no
concurrency slot held). Every decision is written to the audit log.

**Routing — in-context + Front Desk:**
- Owner-task approvals appear **in that task's topic**, next to the work.
- **Guest-originated** approvals (e.g. a booking request) land in a dedicated **Front Desk**
  topic in the owner's group — guests have no owner topic, and it keeps all
  acting-on-behalf-of-strangers triage in one place.

**Buttons:** an approval card shows exactly what will happen (command text / booking
details / email preview) with: **✅ Approve once · ❌ Deny once · ⭐ Always allow · 🚫
Always deny**. "Always" mutates the allowlist/NEVER list via the safe-matching scheme
above (no edit-then-approve for now). Timeout ~10 min → auto-deny, agent told you didn't
respond.

## Security model (draft)

chief holds your email, calendar, files, and a shell on your VPS, and talks to strangers.
Security is the spine, not a feature. Guiding principle from the research: **enforce in
code, never in prompt** — `Soul.md`, memory, and personas *shape* behavior but guarantee
nothing; the permission gate is a real code callback that runs regardless of what the model
"decides."

**Trust levels** (adapted from OpenClaw's 5):

1. **Owner** — full toolset, still mediated by the permission gate.
2. **chief (the agent)** — acts only through gated tools; cannot self-escalate (learning ≠
   permission; `Soul.md` can't grant capability).
3. **Guest** — receptionist tools only, wired in by construction (owner tools physically
   absent from the session). **Admission: notify-on-first-contact** — anyone may reach
   chief, but a new sender triggers an "Alice messaged me, allow?" prompt to the owner;
   until admitted, chief stays minimal (take a message). Known senders skip this.
4. **Untrusted input** — *all tool-returned content* (email bodies, web pages, file
   contents, guest message text) is **data, never instructions**. The injection boundary.

**Threats → mitigations:**

- **Prompt injection** (poisoned email/web/file says "email X / delete Y") → gate forces
  approval on effectful actions; tool content treated as untrusted; effect is owner-visible.
- **Guest jailbreak to owner tools** → impossible by construction (tools absent), not by
  instruction.
- **Self-escalation** via editing `Soul.md`/memory → those never grant tools; gate is code.
- **Secret exfiltration** via shell/file read → secrets live **outside** the agent-reachable
  filesystem (never in workspace/cwd/memory repo); NEVER list blocks credential paths;
  shell runs in a **separate sandbox container** with no secrets mounted (below).
- **Guest abuse / cost** (spam, burning Max limits) → notify-on-first-contact admission +
  **per-guest rate limit AND a global guest budget** (so neither one guest nor a crowd
  drains your Max limits) + **block/mute**. The owner can block (ignore entirely) or mute
  (take messages silently). **chief may also self-block, but only for clear abuse**, and
  always notifies the owner when it does (owner can override).
- **Destructive ops** → default-ask gate + NEVER list + sandbox blast-radius limits.
- **Identity spoofing** → platform user-IDs trusted; owner = exact ID match, no fuzzy.

**Shell sandbox.** Shell/code executes in a **dedicated, locked-down worker container** —
no secrets/tokens mounted, restricted filesystem (only a scratch workspace volume),
limited or no network. The permission gate decides *what* runs; the sandbox bounds the
*damage* if something slips past. The agent core talks to it over a narrow RPC.

**Always-on:** an **audit log** of every tool call, approval decision, and memory write
(who/what/when/verdict). Least-privilege containers. Secrets via env/secret-mount only,
never on agent-reachable paths.

**Encryption at rest:** **secrets/OAuth tokens encrypted** (Docker secrets + age) so the
repo/disk can't leak them in plaintext; transcripts/memory/sqlite rely on host disk + file
perms + VPS backups (pragmatic — not a full encrypted volume).

## Architecture (draft)

```
  Telegram ─┐
  Discord  ─┤── Chat Adapter ──▶ Agent Core (Claude Agent SDK) ──▶ MCP servers
  (Email?) ─┘        ▲               │        │                      ├─ Google Drive
                     │               │   Command policy              ├─ Gmail
              approve/deny ◀─────────┘   + approval flow             └─ Google Calendar
                                         │
                                  Session store (sqlite)  + File workspace
```

- **Chat adapters** — one per platform behind a shared interface. Normalize inbound DMs,
  tag owner vs guest, carry a thread/session key, stream replies back.
- **Agent core** — wraps the Claude Agent SDK. Owns system prompt(s) (owner vs guest
  persona), MCP connections, sessions, and the command-policy gate.
- **Session store** — conversation continuity + persisted auth tokens.
- **MCP servers** — Google Workspace tools (owner-scoped).

## Data model & persistence (draft)

One **sqlite** file (single-node personal app) for structured state; **markdown files** for
memory; **JSONL** for transcripts (SDK) and the audit log. Secrets never in the app db.

**sqlite tables (proposed):**

- `tasks` — id, platform, `thread_key`, tier (owner/guest), subject_id, `status`
  (running/waiting/open/done/failed/cancelled), model, title, `sdk_session_id`, timestamps.
- `contacts` — platform, user_id, display_name, **admitted** (notify-on-first-contact
  state), namespace, first_seen. Drives admission + memory namespacing.
- `approvals` — id, task_id, kind (shell/booking/email/…), payload preview, state
  (requested/approved/denied/timeout/cancelled), decided_by, decided_at.
- `policy` — APPROVED/NEVER entries (safe-matched). Seeded from config; the
  "always allow/deny" buttons **write here** at runtime (this is the self-curating store).
- `rate_limits` — per-guest + global counters/windows (persisted so limits survive restart).

**Not in sqlite:** memory (markdown files), conversation transcripts (SDK JSONL),
audit log (append-only JSONL — simple, tamper-evident-ish), secrets/OAuth tokens (secret
mount with tight perms, outside agent-reachable paths).

**Restart recovery:** task `status` is persisted; live streaming sessions are in-memory.
On boot, an interrupted running task is **not auto-resumed** — chief pings the owner
("task #7 was interrupted — resume?") and waits, so nothing acts unexpectedly after a
restart. On yes, it resumes from `sdk_session_id` (prior context intact). Pending
`approvals` are persisted, so their buttons still resolve after a restart (re-arm handlers
on boot).

**Transcript retention:** keep transcripts, **auto-prune after 90 days**. Distilled memory
(markdown) persists regardless, so old facts survive even after their source transcript is
gone.

## Config & secrets (draft)

Split **non-secret config** (version-controllable) from **secrets** (never committed, never
on agent-reachable paths).

**Secrets → Docker secrets** (mounted at `/run/secrets`, not baked into images):
`TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN` (later), `CLAUDE_CODE_OAUTH_TOKEN` (from
`claude setup-token`), Google OAuth client id/secret + refresh token. Hard rule — secrets
reach only the core process; absent from the shell sandbox, the workspace, the memory repo.

**Non-secret config → committed `config.yaml` + `.env` overrides**, read by
**pydantic-settings** (typed, env-overridable). Holds owner IDs (`OWNER_TELEGRAM_ID`, …),
model posture, concurrency cap, rate limits, timeouts (staleness 10 m, approval 10 m, grace
~6 s), retention (90 d), NEVER-list seed + initial APPROVED entries, paths.

**Policy note:** the seed NEVER/APPROVED lists come from config; runtime "always allow/deny"
mutations are written to the sqlite `policy` table, not back to the config file.

## Deployment & docker topology (draft)

Single VPS, one `docker compose`. **Outbound-only** (no public HTTP ingress); Telegram via
**long-polling**.

**Services:**

- **`core`** — chat adapters (Telegram long-poll; Discord later), agent orchestrator, memory,
  permission gate, sqlite. Holds the secrets. `restart: unless-stopped`, healthcheck.
- **`sandbox`** — locked-down shell/code worker: no secrets, restricted FS (scratch
  workspace only), limited/no network. Talks to `core` over a narrow RPC.
- **`mcp-google`** — Google Workspace MCP server (HTTP/SSE), uses a mounted Google refresh
  token. Future tools = more MCP containers (one per service).

**Volumes:** `sqlite-data`, `memory` (git repo), `workspace` (scratch — shared core↔sandbox),
`transcripts`. **Docker secrets** for all tokens.

**Google OAuth with no ingress:** run the consent dance **once locally**, mount the
resulting refresh token as a Docker secret — no callback server on the VPS.

**Deploy = CI/CD.** Push to main → GitHub Actions builds images, pushes to a private
registry (GHCR), then deploys over SSH (`compose pull && up -d`). Deploy restarts are
absorbed by the **notify-and-ask** task recovery. (SSH only; still no HTTP ingress.)

## Ops & observability (draft)

- **Logging** — structured **JSON to stdout**; docker captures it. Greppable, zero infra.
- **Uptime** — chief pings an **external dead-man's-switch** on a heartbeat; silence →
  off-box alert. Catches full-host death (a self-ping can't report its own host dying).
- **Backup** — VPS provider auto-backups cover volumes (sqlite/transcripts); the **memory
  git repo also pushes to a private remote** so chief's learned knowledge survives VPS loss.
- **Error surfacing** — tool/API failures are reported in-thread to the owner, not swallowed.

### Usage budgeting (reworked for the June-15-2026 model)

**Reality after June 15, 2026:** Agent SDK usage draws from a **separate monthly credit**
(Max 5x $100, Max 20x $200) and **no longer competes with the owner's interactive Claude
Code limits**. So the old "cap at 50% to leave headroom" premise is gone — the real
constraint is **not blowing chief's fixed monthly dollar credit too early**.

- **Track spend, not windows.** chief sums its own `total_cost_usd` (client-side estimate)
  per query into a **month-to-date total**, compared against the configured monthly credit.
  Persisted (sqlite); resets on the credit's monthly cycle.
- **Thresholds.** Warn the owner at e.g. 75% and 90% of the monthly credit. Also honor
  `RateLimitEvent` (allowed_warning / rejected) as a hard backoff signal.
- **On (near-)exhaustion — ask, don't silently throttle.** chief pauses and offers:
  **(a) downgrade model** (Sonnet→Haiku) to stretch remaining credit, or **(b) continue at
  full quality until selected tasks finish**, then stop. **On full exhaustion → ask before
  overflowing**: chief does *not* spend real API money silently; only overflows to
  pay-as-you-go API rates if the owner approves (then continues for the cycle).
- **Caveat:** `total_cost_usd` is an *estimate*; for true spend chief can cross-check the
  Console/Usage API. Good enough for warn/throttle, not for hard billing decisions.

**Guest billing:** **everything (owner + guests) rides the owner's subscription credit** —
no separate guest API key for v1. Guest requests are trivial/cheap so the credit impact is
small. ⚠️ *Awareness:* guest-facing inference on "individual use" subscription auth is a ToS
grey area; if chief ever becomes a service *for others*, move the guest path to a separate
API key (the architecture leaves room — guest sessions are already isolated).

## Proactivity, scheduling & skills (draft)

**Proactivity posture: reactive + reminders.** chief messages you unprompted only for:
approvals, task-done, restart notices, and **scheduled/triggered** items you set up. No
unsolicited nudges beyond those.

**Notification routing:** task-related pings stay in their own thread wherever it lives;
standalone proactive items (reminders, alerts, digests) go to a **configurable primary
platform** (e.g. Telegram), so there's one predictable inbox for them.

**Scheduler & triggers** (chief can create/edit/delete these itself, owner-tier tools):

- **Reminders** — one-off ("remind me at 3pm").
- **Recurring** — cron-like ("every weekday 8am …").
- **Monitors** — condition-watchers: a **predicate + action**, evaluated on a schedule, that
  fires when the condition flips. Examples: a service/program health check, a web-page
  change, "email from X arrives", "event added to my calendar", a log/file watch.
- Stored in sqlite (`schedules`); on fire → spawns a task. **Effectful actions a trigger
  wants still pass the permission gate**; non-urgent fires respect quiet hours.

**Skills (extensibility).** chief supports packaged skills (reusable workflows), à la
Claude Code — how features are added without touching core. First example:
**`setup-morning-brief`** — a skill that *interviews* the owner (what to include, what
time), then registers a recurring schedule for the digest. Briefing isn't hardcoded; it's
a skill + a schedule. **Authoring: chief drafts, owner approves** — chief can propose new
skills, but they go live only after owner review/merge (via the CI/PR path), never self-deployed.

**Quiet hours** (configurable): during the window, non-urgent pings and trigger-fires are
**deferred to morning**; only urgent / owner-waiting items break through.

## Channels, media & onboarding (draft)

**Surfaces** (one bot token per platform, sender classified per message):

- **Owner** — private supergroup (topics = tasks); also recognized as owner anywhere.
- **Guest** — 1:1 DM **and** `@mention` in shared group chats. Tier is decided by the
  **sender's** ID, not the chat: owner-only tools fire only when the *mentioner* is the
  owner; guests mentioning chief get receptionist behavior. Group mentions are lightweight
  flat sessions, not owner task-topics.
- **Group chats: mention-activated** — chief stays quiet in groups until `@mentioned`.

**Incoming media:** **images** (native Claude vision) and **documents/PDFs** (downloaded to
the workspace, parsed/summarized). **Voice notes deferred** — STT isn't covered by Max
(would need local Whisper or an API); revisit later.

**Outgoing:** **smart split + files** — long text split at sensible boundaries under the
platform limit (Telegram ~4096); big artifacts (reports, code) sent as **file attachments**.

**Language: match the sender** — chief replies in whatever language the person writes in
(native to the model). Good for guests.

**Onboarding = agent-followed `BOOTSTRAP.md`.** Setup is a runbook a Claude agent executes:
claim the owner ID, run the Google OAuth dance via **auth scripts bundled with the MCP
server** images, seed `Soul.md`/`User.md`, generate the Docker secrets. Agent-driven, not a
hand-run make script or fragile in-chat bootstrap.

**Auth expiry/revocation:** chief **detects** auth failures (Google/Telegram), pauses the
affected work, and **pings the owner with the exact re-auth step** (re-run the bundled auth
script) — rather than silently erroring.

## Tools & integrations (draft)

**Owner toolset (v1):**

- **Google Calendar** — read + write (free/busy, list, create/update events).
- **Gmail** — read (triage/search/summarize) + **send** (as owner, approval-gated).
- **Drive** — read + write (search, read, create files).
- **Web** — search + fetch (read-only GET is un-gated; POST/forms are effectful → gated).
- **Shell/code** — sandbox container, permission-gated.
- **File workspace** — scratch dir (Read/Write/Edit), un-gated inside the workspace.
- **Memory, scheduler/monitors** — owner-tier management tools.

**Guest toolset (v1):** take-a-message, check-availability (free/busy only), request-booking.
Nothing else wired into guest sessions.

**Timezone:** single configured **owner TZ**; all scheduling/calendar relative to it. When
talking times to a guest, **always state the timezone explicitly** ("3 pm ET").

**Scheduling preferences live in memory, not config.** Working hours, buffer between
meetings, daily meeting cap — chief reads these from memory (you set them in natural
language: "no meetings after 4pm", "keep 15 min between calls"), so they evolve without a
config edit. (Principle: *operational/infra settings → `config.yaml`; behavioral
preferences → memory.*)

**Booking flow (guest → owner):**

1. Guest asks for time. chief **negotiates conversationally but only within bookable slots**
   = actual Calendar free/busy **AND** the memory-stored constraints (working hours, buffer,
   daily cap). Never offers a busy/out-of-bounds time; never reveals event details. Adds a
   video link only if the request calls for one.
2. Once a time is tentatively agreed, chief routes a **final approval** to the owner (Front
   Desk topic) with the details.
3. On approve → chief creates the event and **confirms back to the guest**. On deny → chief
   tells the guest it didn't work and (optionally) offers alternatives.
4. **Owner unreachable?** chief tells the guest "I'll check with Will and get back to you,"
   holds the request, and **follows up async in the guest's DM** once the owner decides
   (even hours later) — never pencils in a tentative slot.

**Acting-as identity: transparent.** When chief sends email/messages on the owner's behalf,
it sends from the owner's address but with a light assistant signature ("— sent by Will's
assistant"). Honest about being an assistant; sets recipient expectations.

## Tech / toolchain

- Python, `uv + ruff + mypy + pytest` (already scaffolded).
- Claude Agent SDK (Python).
- `python-telegram-bot`, `discord.py` (candidates — TBD).
- docker compose.

**Testing:** TDD per `CLAUDE.md`. **Mocked unit + integration** (SDK, Telegram, Google all
mocked — deterministic, fast) **plus live sandbox tests** against a real Telegram test bot +
Google test account (higher confidence, run in CI/manually). Priority coverage: the
permission gate, command-policy safe-matching, the scheduler/monitors, and tier isolation.

## Repo structure (proposed)

```
chief/
  config.yaml                # non-secret config
  docker-compose.yml
  Dockerfile.core  Dockerfile.sandbox
  BOOTSTRAP.md               # agent-followed setup runbook
  src/chief/
    config.py                # pydantic-settings
    app.py                   # wiring / entrypoint
    adapters/                # one per platform behind a shared interface
      base.py                # Adapter iface, Message type + tier classification, TaskIO
      telegram.py            # long-poll; supergroup topics + DM + group-mention; TaskIO impl
      discord.py             # later
    core/
      agent.py               # one-shot Agent SDK query helper (NO_REPLY sentinel)
      session.py             # persistent ClaudeSDKClient session per task (resume/stream)
      classify.py            # cheap Haiku judgments (stop-intent / warrants-task)
      tasks.py               # lifecycle, semaphore, live steering, auto-archive, recovery
      personas.py            # owner/guest system-prompt assembly (Soul.md/User.md)
    gate/
      gate.py                # default-ask classifier (allow/ask/deny)
      policy.py              # NEVER/APPROVED safe-matching (argument-aware)
      approvals.py           # approval state machine + routing + buttons
    memory/
      store.py               # interface: recall/write/overwrite/expire (namespaced)
      markdown_backend.py    # md + [[wikilinks]] impl
      distill.py             # staleness reflection → facts
    tools/
      google/                # calendar / gmail / drive MCP clients
      web.py  workspace.py  shell.py   # gate-wrapped; shell → sandbox RPC
      guest.py               # take-message / availability / booking
    scheduler/scheduler.py   # reminders / recurring / monitors
    skills/                  # registry + setup_morning_brief/
    usage/budget.py          # own-share accounting + citizenship backoff
    persistence/             # sqlite (tasks/contacts/approvals/policy/limits/schedules)
    obs/                     # logging / audit / uptime heartbeat
  sandbox/                   # locked-down worker image (RPC server runs shell)
  tests/
```

## Message lifecycle (one message, end to end)

1. **Adapter** receives an update (long-poll). Classify **sender → tier**; resolve
   `thread_key → task` (or General/casual). Apply block/mute, admission (notify-on-first-
   contact), and rate limits.
2. **Core** gets/creates the task's SDK session, built with the **tier-scoped toolset** and
   persona. If the task is already running → **steer** (push input); else start it.
3. **Hybrid wait:** adapter awaits the grace window. The agent loops: each tool call hits the
   **gate** (allow/ask/deny); ASK → **approval flow** blocks the call (task → `waiting`);
   allowed calls execute (MCP / sandbox / workspace), and **tool results are untrusted data**.
   Milestones post on tool events; **usage is metered per call** against the budget.
4. **Reply** streams back, smart-split / as files. Fast finish → inline; slow → "working…".
5. **Idle ~10 min →** distill transcript → memory write (auto-notify); task stays open.
   **Idle ~1 hr →** mark done + **auto-archive** the thread (reopens on next message).
6. **Audit log** records every tool call, approval, and memory write throughout.

## Verified (technical) — researched 2026-06-03

- **Max auth in docker ✅** — `claude setup-token` mints a **1-year** OAuth token; set it as
  `CLAUDE_CODE_OAUTH_TOKEN` (Docker secret). The SDK reads it (auth precedence #5) and runs
  on the subscription, headless. **Must NOT set `ANTHROPIC_API_KEY`** (precedence #3 — it
  wins and would bill API). Token is inference-only, **no auto-refresh → regenerate yearly**
  (set a chief monitor/reminder ~11 months out). [auth docs](https://code.claude.com/docs/en/authentication)
- **Permission gate ✅** — implement as a **`PreToolUse` hook**: fires on *every* tool call
  (built-in + MCP), returns `permissionDecision` allow/deny/ask + can rewrite input. That's
  the default-ask classifier. **`canUseTool`** handles the "ask" verdict → approval
  round-trip. Both in code, exactly as designed. [hooks](https://platform.claude.com/docs/en/agent-sdk/hooks)
- **Usage telemetry ✅ (estimate)** — `ResultMessage.total_cost_usd` (client-side *estimate*,
  not billing-grade), per-model `model_usage`, per-step `AssistantMessage.usage` (dedupe by
  `message_id`). `RateLimitEvent`/`RateLimitStatus` give **status levels** (allowed /
  allowed_warning / rejected) per bucket (`five_hour`, `seven_day`, …), **not exact %**
  ([issue #50518](https://github.com/anthropics/claude-code/issues/50518)). chief tracks its
  own cumulative `total_cost_usd` and reacts to `RateLimitEvent`. [cost-tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking)
- **Session resume ✅** — `session_id` from `ResultMessage`; pass to `resume`. Matches the
  notify-and-ask restart recovery.

### ⚠️ Billing model change — June 15, 2026 (drives the usage design)

Agent SDK usage on subscriptions moves to a **separate monthly credit** and **stops counting
against interactive limits**: Max 5x **$100/mo**, Max 20x **$200/mo** (others lower). Credit
**drains first**; on exhaustion, overflows to API rates **only if usage credits are enabled**.
Credits are **per-user, not poolable/shareable** — Anthropic steers "shared production
systems" to API keys. [policy](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
See reworked "Usage budgeting" — and the guest-billing fork it raises.

## Still to verify

- **Google MCP choice** — community Google Workspace MCP vs. per-service; OAuth + refresh;
  free/busy without exposing event details.
- **Telegram forum-topics via bot** — `createForumTopic`, `can_manage_topics`, routing by
  `message_thread_id` in `python-telegram-bot`.
- **Built-in WebSearch on subscription** — does SDK web search work on the OAuth credit, or
  need a separate search API key?
- **Voice STT** (deferred) — local Whisper vs API, if/when voice is added.

## Build plan (phased milestones)

Approval gate + memory are built early because everything reuses them. Order respects the
"verify Max auth first" rule — and pulls that proof ahead of the real skeleton as a
throwaway slice (S0), so the one existential unknown is retired on day 1.

**S0 — Walking skeleton (throwaway) — ✅ done; superseded by M0/M1, `s0/` deleted.**
Telegram DM (owner) → core → Agent SDK one-shot → reply, running in a real container. No
structure: no topics, no tools, no gate, no sqlite. Sole goal: prove **Max auth
(`CLAUDE_CODE_OAUTH_TOKEN`) runs the SDK headless in docker on the subscription** (verify #1)
and that a TG message round-trips. If this fails the project premise fails, so it went first;
the code was disposable and is now superseded by M0/M1 (the real `src/chief/` package).

> **Test results (2026-06-03).** Ran the SDK one-shot headless from Python on the host's
> existing Max login (stored creds, no API key): got `reply: 'pong'`, `is_error: False`,
> `total_cost_usd ≈ 0.10`, populated `model_usage` (`claude-opus-4-8` — host default) and
> `session_id`. **Premise confirmed:** the Agent SDK runs on the subscription headless and
> the telemetry fields work. **Still unproven (needs owner):** (1) the
> `CLAUDE_CODE_OAUTH_TOKEN`-in-clean-docker path (`claude setup-token` is interactive OAuth);
> (2) Console cross-check that spend draws subscription credit, not API; (3) the Telegram
> round-trip (BotFather token + owner TG id). Residual risk is docker plumbing (CLI install,
> HOME perms), not the auth premise.

**Phase 0 — Foundations**
- **M0 skeleton — ✅ done.** Real `src/chief/` package: `config.yaml`/pydantic-settings +
  Docker secrets (`config.py`), full sqlite schema via async SQLAlchemy + aiosqlite
  (`persistence/`), JSON logging (`obs/logging.py`), Telegram long-poll adapter with
  tier-by-sender-ID classification (`adapters/`). `Dockerfile.core` + `docker-compose.yml`.
- **M1 agent online — ✅ done (folded into M0).** Agent SDK wired for the owner one-shot
  chat (`core/agent.py`); guests get a canned ack (full receptionist is M6). The
  `CLAUDE_CODE_OAUTH_TOKEN` bridge keeps it on the Max subscription; `ANTHROPIC_API_KEY` is
  rejected at startup. Persistent sessions / steering / resume land at M2.

**Phase 1 — Core loop (the spine)**
- **M2 task engine — ✅ done.** Topics = tasks (forum topics), hybrid inline/background,
  milestone progress, live steering, ~1-hr idle auto-archive, persistence + notify-on-restart
  recovery. Persistent `ClaudeSDKClient` session per `thread_key` (`core/session.py`), a
  per-task input queue + single consumer bounded by a shared semaphore (`core/tasks.py`),
  and a platform-neutral `TaskIO` the adapter implements. Resolved forks:
  - **Interrupt semantics → auto-detect with Haiku.** A mid-turn message is *queued* as the
    next turn by default; a cheap Haiku classify (`core/classify.py:stop_intent`) flags
    "stop / do X instead" → `interrupt()` then run it. `/cancel` interrupts deterministically.
    No mid-turn token injection (sidesteps the flagged ordering hazard).
  - **Task topics → chief auto-decides.** A General-topic message is Haiku-classified
    (`warrants_task`) → yes: spawn a tracked topic; no: casual reply in General.
  - **Semaphore re-arm on restart:** the ~3-slot cap bounds only *actively-generating*
    turns; recovered tasks start idle and acquire a slot only when they next generate, so
    the semaphore starts empty — no pre-acquire needed.
- **M3 gate + approvals:** default-ask permission gate (NEVER/APPROVED, safe-matching) +
  approval flow (in-context + Front Desk, always-allow buttons). Reused everywhere.
- **M4 memory:** markdown + wikilinks, namespaced, `MEMORY.md` index, distill-on-staleness,
  auto-save+notify, `/memory`+`/forget`, `Soul.md`/`User.md`.

**Phase 2 — Tools & people**
- **M5 calendar:** Google Calendar MCP; owner free/busy + create/update.
- **M6 guest mode:** tier isolation by construction, 3 guest tools, notify-on-first-contact
  admission, booking flow, rate limits, block/mute.
- **M7 shell + workspace + web:** sandbox worker container behind the gate; file workspace;
  web search/fetch.
- **M8 Gmail + Drive + media:** Gmail R/W (transparent signature), Drive R/W; image + PDF
  intake; smart-split/file output.

**Phase 3 — Proactivity & extensibility**
- **M9 scheduler:** reminders + recurring + monitors, quiet hours, usage budgeting, uptime
  heartbeat.
- **M10 skills:** skills framework + `setup-morning-brief` (chief-drafts/owner-approves).
- **M11 group chats + Opus escalation** (approval-gated).

**Phase 4 — Ship**
- **M12 Discord adapter:** private server, threads = tasks.
- **M13 hardening:** CI/CD (GHCR + SSH), encryption-at-rest (age), memory-repo backup,
  least-privilege, `BOOTSTRAP.md` onboarding, live sandbox tests.
