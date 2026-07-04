# chief — Design

A personal AI agent, running natively on your own machine. DM it from Telegram or
Discord and it does things for you — and for other people, like a real assistant
screens for its boss. Backed by your Claude Max subscription via the Claude Agent SDK.

Status: **design complete (v1)**, built through M13 — and **reworked host-native
(2026-07)**, see the next section.

## Host-native rework (2026-07)

An owner-approved architectural reversal from the original fully-containerized,
sandbox-isolated design to a host-native, highly-autonomous one. What changed:

- **Core runs natively on the host** (Linux + macOS): `chief` /
  `uv run python -m chief.entrypoint` from the repo root. Only the MCP sidecars
  (Google servers, playwright) remain in docker compose, publishing their ports on
  `127.0.0.1` for core to reach over localhost. Secrets are plain files in a secrets
  dir (`./secrets` or `~/.config/chief/secrets`) or env vars; data lives in the
  gitignored repo-local `data/`.
- **The shell is real.** The owner's bash tool spawns persistent per-task shells
  directly on the host (`$SHELL`, else bash, else zsh) with the full environment —
  the sandbox container and its RPC are gone. The owner accepts that shell children
  see the process env (OAuth token included).
- **Gate flipped to default-allow (owner tier).** Effectful owner calls run freely;
  only an **approval blacklist** (configurable regexes over shell commands + tool
  names: sudo, `rm -rf` on a root, disk writes, power state, pipe-to-shell,
  force-push main, global installs, …) still raises the approval card. NEVER and the
  self-curating APPROVED list are kept. Guests are exactly as locked down as before.
- **Untrusted-content screening** replaces container isolation as the injection seam:
  web/browser tool results and guest messages pass a cheap Haiku screen; hits are
  annotated with a warning (optionally blocked).
- **No prod.** The VPS deploy is gone; chief ships as a local install (`install.sh`).

Sections below marked *(superseded — host-native)* keep the original design for the
record; the rest of this section is the current truth.

## Goals

- Reach the agent by DM from **Telegram** and **Discord** (Telegram first).
- **Two tiers of caller:** one privileged **owner** per platform = you; everyone else is
  a **guest**, explicitly *not you*, and gets a restricted "assistant" experience.
- Agent acts via MCP servers: **Google Drive, Gmail, Google Calendar** (owner only).
- Plus **web search/fetch**, a **file workspace**, and a real **shell** on the owner's
  machine (blacklist-gated).
- Use the **Claude Agent SDK** (Python) so it runs on **Claude Max**, not API billing.
- Ship as a **local install**: native core + a small docker compose stack of MCP
  sidecars.

## Design completeness — areas to settle

Tracking the full surface so "done designing" is well-defined. ✅ settled · 🚧 in progress · ⬜ todo.

- ✅ Identity & access (owner/guest tiers)
- ✅ Session memory (task = thread/topic)
- ✅ Guest scope + booking approval
- ✅ Command-policy tiers (values of the lists still TBD)
- ✅ Task execution engine — hybrid, bounded-parallel, milestones, live steering
- ✅ Agent core internals — tier isolation, tool exposure, model, Soul.md
- ✅ Approval flow — unified gate, default-ask, self-curating (deterministic) buttons
- ✅ Long-term memory & learning — markdown + wikilinks, namespaced, auto-generated facts listing
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
(TG topics API, websearch-on-credit, STT) are routine build-time spikes.

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
| Permission gate | One gate, tier-split (host-native): **owner default-allow** — NEVER deny · APPROVED allow · blacklist ASK · else allow; **guest default-ask** unchanged |
| Approval routing | In task topic for owner work; **Front Desk** topic for guest-originated |
| Approval buttons | Approve/Deny once + Always-allow/deny (self-curating allowlist) |
| Models | Owner: Sonnet default, Opus on demand. Guests: Sonnet, never Opus |
| Personality | `Soul.md` — chief-editable, git-tracked, loaded into every prompt |
| Long-term memory | Markdown + `[[wikilinks]]` (graph-lite), namespaced facts files; `facts/` listing auto-generated at prompt-build time; chief writes in-session via Write tool; auto-save+notify; no embeddings; mem0 swap deferred behind an interface |
| Memory mechanics | Overwrite stale facts; TTL-flag time-sensitive; provenance per fact; keep full transcripts; every write git-versioned |
| Tier isolation | Guest sessions built with *only* guest tools (not prompt-instructed) |
| Guest admission | Notify-on-first-contact (owner allows new senders; known senders skip) |
| Guest rate limits | Per-guest cap + global guest budget (protect Max limits) |
| Shell | Host-native (2026-07): persistent per-task shells on the owner's machine, full env; blacklist-gated (was: sandbox container) |
| Audit | Log every tool call, approval decision, memory write |
| Persistence | One sqlite (tasks/contacts/approvals/policy/limits) + md memory + JSONL transcripts/audit |
| Restart | Interrupted tasks: notify + ask before resuming. Approvals survive restart |
| Transcripts | Keep, auto-prune after 90 days; memory written directly by chief persists |
| Transport / ingress | Telegram long-polling; outbound-only, no public HTTP ingress |
| MCP servers | chief's **own FastMCP** servers, one container per service (streamable-HTTP on `127.0.0.1:<port>`) — per-connection transport sidesteps the vendored nspady "Server already initialized" collision |
| Deploy | Local install (host-native): `install.sh` + `chief` launcher; CI keeps done-check + smoke test, no VPS deploy |
| Logging / uptime | JSON to stdout; external dead-man's-switch heartbeat |
| Backup | VPS auto-backups + memory git repo pushed to private remote |
| Auth | `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` (secrets-dir file or env); never set `ANTHROPIC_API_KEY`; regen yearly |
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
| Google scope (v1) | Calendar R/W, Gmail R/W, Drive R/W, Sheets R/W (row-1 header guard) |
| Web (v1) | Search + fetch (GET un-gated, POST gated) |
| Timezone | Single configured owner TZ |
| Booking | Negotiate within free/busy + memory prefs (hours/buffer/cap); video link only if asked; final owner approval; confirm guest |
| Sched prefs | Live in **memory** (natural language), not config; behavioral prefs → memory, infra → config.yaml |
| Away status | A time-sensitive memory fact ("on vacation til Mon"), not a mode |
| Guest times | Always owner TZ, timezone stated explicitly |
| Opus escalation | Always owner-approved: command pre-approves, auto-detect asks |
| Skill authoring | chief drafts, owner approves via review/merge; never self-deployed |
| Acting-as identity | Transparent — assistant signature on outgoing email/messages |
| Encryption at rest | Secrets = `0600` files in the secrets dir; transcripts/memory via host perms + backups |
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
metadata lives in sqlite; **auth tokens do not** — they're files in the secrets dir
(see Config & secrets).

## Task execution engine

What happens between "you send a task" and "it's done." **Decided:** hybrid execution,
bounded parallelism, milestone progress, live steering.

- **Live background sessions.** Every task runs as a live background session from the
  start, posting milestones as it works. All surfaces stay silent until the reply
  streams back. One mechanism, every surface.
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
  by id). Keeps the list tidy.
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
  - **Shell** → chief's in-process `mcp__chief_shell__bash` tool (the SDK built-in
    Bash stays refused: one shell surface), running persistent per-task shells on the
    host, blacklist-gated. Owner-only.
  - **File workspace** → SDK built-in Read/Write/Edit; host-native: unconfined for the
    owner, with `data/workspace` as the suggested scratch dir + shell cwd. Owner-only.
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
- **`facts/owner/…`**, **`facts/contacts/<id>/…`** — one fact (or small cluster) per file,
  with frontmatter (`trust`, `expires`, provenance). `[[wikilinks]]` connect them (entities
  = files, relations = links) — relational traversal without a graph DB.

The `facts/` listing is **auto-generated by scanning the directory at prompt-build time**
(no stored index file). Chief sees the listing in every owner session and opens individual
fact files on demand (+ grep as a cheap fallback). No vector search until/unless scale
demands it. Every write is **git-versioned** for reversibility.

**Decided in discussion (mechanics, substrate-independent):**

- **Namespaced.** Memory is partitioned by subject: owner facts vs per-contact facts.
  Guest-stated facts are fenced into that contact's namespace and tagged low-trust.
- **In-session writes.** Chief writes facts directly during the session via its
  memory-confined Write tool whenever something worth remembering comes up. Full transcripts
  are kept (SDK behavior). Task auto-archive fires at ~1-hr idle (closes the topic, reopens
  on next message). *(Distill-on-staleness was designed but retired before ship — M4 ships
  direct in-session writes instead.)*
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

## Permission gate (default-allow + blacklist — host-native)

One gate classifies **every** tool call, split by tier (2026-07 rework; the original
default-ask posture survives unchanged on the guest tier).

**Owner tier — default-allow.** Decision order:

1. **NEVER list** → hard refuse, no prompt, no override. Still covers the truly
   destructive Google ops (per-service `deferred_tools`, also refused at the SDK
   layer) and anything the owner "always-denies".
2. **APPROVED allowlist** → **allow**, no prompt — an explicit blessing (the
   "always allow" button) beats the blacklist.
3. **Approval blacklist** → **ASK** (the approval flow). Configurable in
   `config.yaml`: regexes over shell commands (`blacklist_shell_patterns` — default
   set: sudo/doas, `rm -rf` on `/`/`$HOME`/`~`, mkfs / `dd of=/dev/`,
   shutdown/reboot, `kill -9 1`, `curl … | sh`, `git push --force` to main/master,
   `chmod -R 777`, global package installs) plus whole tool names
   (`blacklist_tools`).
4. **Everything else** → **allow**. Writes anywhere on the host, sends, bookings —
   the owner chose autonomy; the audit log still records every call.

**Guest tier — default-ask (unchanged):** NEVER → deny · file ops → deny · read-only →
allow · APPROVED → allow · else effectful → ASK. Guests still never have owner tools
wired in at all.

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

- **Prompt injection** (poisoned email/web/file says "email X / delete Y") →
  **untrusted-content screening** (host-native): web/browser results and guest
  messages pass a cheap Haiku screen and a hit is annotated with a warning (or
  blocked, `screening_block`); the blacklist still cards the destructive shapes; tool
  content treated as untrusted; every call is audited.
- **Guest jailbreak to owner tools** → impossible by construction (tools absent), not by
  instruction.
- **Self-escalation** via editing `Soul.md`/memory → those never grant tools; gate is code.
- **Secret exfiltration** via shell/file read → **accepted risk (host-native)**: the
  shell runs as the owner's user with the full environment, OAuth token included —
  the owner explicitly traded this isolation away for autonomy. The blacklist +
  screening + audit log are the remaining controls.
- **Guest abuse / cost** (spam, burning Max limits) → notify-on-first-contact admission +
  **per-guest rate limit AND a global guest budget** (so neither one guest nor a crowd
  drains your Max limits) + **block/mute**. The owner can block (ignore entirely) or mute
  (take messages silently). **chief may also self-block, but only for clear abuse**, and
  always notifies the owner when it does (owner can override).
- **Destructive ops** → approval blacklist (cards the classic destructive shapes) +
  NEVER list. No sandbox blast-radius bound anymore — blacklist coverage is the line.
- **Identity spoofing** → platform user-IDs trusted; owner = exact ID match, no fuzzy.

**Shell (host-native).** Shell/code executes **directly on the owner's machine** as
their user: a persistent per-task shell (`$SHELL`, else bash, else zsh) with the full
environment, cwd at the workspace dir, per-command timeout + output cap. The approval
blacklist decides *what still needs a tap*; there is no container bounding the damage —
that is the deliberate trade. *(Superseded: the M7 sandbox container + RPC.)*

**Always-on:** an **audit log** of every tool call, approval decision, and memory write
(who/what/when/verdict). Least-privilege containers. Secrets via env/secret-mount only,
never on agent-reachable paths.

**Encryption at rest:** secrets are `0600` files in the secrets dir (or env vars);
transcripts/memory/sqlite rely on host disk + file perms + backups (pragmatic — not a
full encrypted volume).

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

**Secrets → a secrets dir** (one file per secret; host-native): the first existing of
`$CHIEF_SECRETS_DIR`, `~/.config/chief/secrets`, or the repo-local `./secrets`, read by
pydantic-settings; env vars of the same name always work. Holds `telegram_bot_token`,
`discord_bot_token`, `claude_code_oauth_token` (from `claude setup-token`), the Google
OAuth client + tokens (`secrets/google_tokens/`). Files are `0600`. Note the shell now
runs on the host, so secrets in the process env are visible to shell children — an
accepted trade (see Security model).

**Non-secret config → committed `config.yaml` + `.env` overrides**, read by
**pydantic-settings** (typed, env-overridable). Holds owner IDs (`OWNER_TELEGRAM_ID`, …),
model posture, concurrency cap, rate limits, timeouts (staleness 10 m, approval 10 m, grace
~6 s), retention (90 d), NEVER-list seed + initial APPROVED entries, paths.

**Policy note:** the seed NEVER/APPROVED lists come from config; runtime "always allow/deny"
mutations are written to the sqlite `policy` table, not back to the config file.

## Deployment & topology (host-native)

One machine — the owner's. **Outbound-only** (no public HTTP ingress); Telegram via
**long-polling**, Discord via the gateway.

- **core** — runs **natively on the host**: `chief` (the installed launcher) or
  `uv run python -m chief.entrypoint` from the repo root, which migrates the DB then
  execs `chief.app`. Data (sqlite, audit log, memory git repo, workspace, screenshots)
  lives in the gitignored repo-local `data/`.
- **`mcp-calendar` / `mcp-drive` / `mcp-sheets` / `mcp-gmail`** — chief's own FastMCP
  servers in docker compose behind the **`google` profile**, each publishing its port
  on **`127.0.0.1` only** (:8003/:8001/:8002/:8004); core connects via localhost. One
  shared Google token dir (`secrets/google_tokens/`) is bind-mounted into all four;
  `mcp-sheets` is the sole writer.
- **`mcp-playwright`** — the browser sidecar behind the `playwright` profile,
  `127.0.0.1:3000`; screenshots land in `./data/screenshots` for core to deliver.

**Google OAuth:** run the consent dance **once locally**
(`python -m chief.tools.google.auth`) — the token lands in `secrets/google_tokens/`.

**Install = `install.sh`** — checks prerequisites (git, uv, docker+compose), scaffolds
`data/` + `secrets/`, `uv sync`, runs migrations, optionally
`docker compose --profile google --profile playwright up -d --build`, and installs the
`chief` launcher to `~/.local/bin`. **No prod:** CI runs the done-check + a host smoke
test (migrate-before-app + compose validity); the VPS deploy job is gone.

## Ops & observability (draft)

- **Logging** — structured **JSON to stdout**; docker captures it. Greppable, zero infra.
- **Uptime** — chief pings an **external dead-man's-switch** on a heartbeat; silence →
  off-box alert. Catches full-host death (a self-ping can't report its own host dying).
- **Backup** — back up the repo-local `data/` dir (sqlite/audit/memory); the **memory
  git repo can also push to a private remote** so chief's learned knowledge survives
  machine loss.
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

**Owner toolset (v1).** Google services are chief's **own per-service FastMCP servers**
(`docker/mcp-*`), each pre-approving its reads and routing its writes through the approval
card:

- **Google Calendar** — read (free/busy, list, get-event, current-time) + write
  (create/update events). Delete is deferred (hard-blocked). *Live.*
- **Gmail** — read (triage/search/summarize) + **send** (as owner, approval-gated). *M8.*
- **Drive** — read a Doc/PDF/Office file, and render+upload a Markdown file as PDF
  (approval-gated). *Server built (M5), disabled until M8.*
- **Google Sheets** — read (ranges/formulas/listings) + write (cells/rows/sheets, share),
  with a server-side **row-1 (header) write guard**; first-party FastMCP server
  (`docker/mcp-sheets/server.py`), multi-account per-request credential selection,
  atomic per-account token write-back. *Live (M8).*
- **Web** — search + fetch (read-only GET is un-gated; POST/forms are effectful → gated).
  SDK built-ins (WebSearch/WebFetch). *Live (M7).*
- **Shell/code** — persistent per-task shells on the host, blacklist-gated (host-native
  rework; was the M7 sandbox container). Opt-in (`shell_enabled`).
- **File workspace** — scratch dir (Read/Write/Edit) at `data/workspace`; host-native:
  writes anywhere are allowed, the workspace is just the suggested scratch area +
  shell cwd. Opt-in (`workspace_enabled`).
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
- `python-telegram-bot`, `discord.py` (both in use — Telegram long-poll + Discord gateway).
- docker compose.

**Testing:** TDD per `CLAUDE.md`. **Mocked unit + integration** (SDK, Telegram, Google all
mocked — deterministic, fast) **plus live sandbox tests** against a real Telegram test bot +
Google test account (higher confidence, run in CI/manually). Priority coverage: the
permission gate, command-policy safe-matching, the scheduler/monitors, and tier isolation.

## Repo structure (proposed)

```
chief/
  config.yaml                # non-secret config
  docker-compose.yml         # MCP sidecars only (host-native core)
  install.sh                 # local install: prereqs, scaffold, deps, migrations, launcher
  BOOTSTRAP.md               # agent-followed setup runbook
  src/chief/
    config.py                # pydantic-settings
    app.py                   # wiring / entrypoint
    adapters/                # one per platform behind a shared interface
      base.py                # Adapter iface, Message type + tier classification, TaskIO
      telegram.py            # long-poll; supergroup topics + DM + group-mention; TaskIO impl
      discord.py             # gateway; channel threads = tasks; TaskIO + ApprovalIO impl
    core/
      agent.py               # one-shot Agent SDK query helper (NO_REPLY sentinel)
      session.py             # persistent ClaudeSDKClient session per task (resume/stream)
      classify.py            # cheap Haiku judgments (stop-intent / warrants-task)
      tasks.py               # lifecycle, semaphore, live steering, auto-archive, recovery
      personas.py            # owner/guest system-prompt assembly (Soul.md/User.md)
    gate/
      gate.py                # tier-split classifier (owner default-allow / guest default-ask)
      blacklist.py           # the approval blacklist (shell regexes + tool names)
      policy.py              # NEVER/APPROVED safe-matching (argument-aware)
      approvals.py           # approval state machine + routing + buttons
    memory/
      store.py               # interface: recall/write/overwrite/expire (namespaced)
      markdown_backend.py    # md + [[wikilinks]] impl; facts/ listing auto-generated
      versioning.py          # git-versions every memory write (reversibility)
    tools/
      google/auth.py         # host-only one-time OAuth → shared token (cal+drive+sheets)
      calendar/mcp.py  drive/mcp.py  sheets/mcp.py   # per-server tool catalogs (read/write split)
      shell.py               # owner bash → persistent per-task host shells (host-native)
      guest.py               # M6: GuestService (leave_message relay) + GuestAdminService (manage_guest); availability/booking reuse calendar/mcp.guest_service
    scheduler/scheduler.py   # reminders / recurring / monitors
    skills/                  # registry + setup_morning_brief/
    usage/budget.py          # own-share accounting + citizenship backoff
    persistence/             # sqlite (tasks/contacts/approvals/policy/limits/schedules)
    obs/                     # logging / audit / uptime heartbeat
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
   allowed calls execute (MCP / host shell / workspace), and **tool results are untrusted
   data** (screened for injection when they arrive from the web/browser).
   Milestones post on tool events; **usage is metered per call** against the budget.
4. **Reply** streams back, smart-split / as files. All surfaces stay silent until the
   reply streams back.
5. **Memory writes** happen in-session whenever chief judges something worth keeping (auto-
   notify on save). **Idle ~1 hr →** mark done + **auto-archive** the thread (reopens on
   next message).
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
- **Google MCP choice ✅ (resolved at M5)** — chief builds its **own per-service FastMCP
  servers** (calendar/drive/sheets) instead of a single community/Workspace server. FastMCP
  gives each connection its own transport, so the per-task MCP connections chief opens don't
  collide ("Server already initialized" — the failure mode of the vendored nspady `mcp-gcal`,
  now replaced). Reads pre-approved, writes approval-gated; one shared OAuth token, minted
  locally, covers all three scopes.

### ⚠️ Billing model change — June 15, 2026 (drives the usage design)

Agent SDK usage on subscriptions moves to a **separate monthly credit** and **stops counting
against interactive limits**: Max 5x **$100/mo**, Max 20x **$200/mo** (others lower). Credit
**drains first**; on exhaustion, overflows to API rates **only if usage credits are enabled**.
Credits are **per-user, not poolable/shareable** — Anthropic steers "shared production
systems" to API keys. [policy](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
See reworked "Usage budgeting" — and the guest-billing fork it raises.

## Still to verify

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
- **M3 gate + approvals — ✅ done.** Default-ask permission gate (`PreToolUse` hook) +
  `canUseTool` approval flow, NEVER/APPROVED **safe-matching** (argument-aware,
  metacharacter-rejecting), in-context + Front Desk routing, 4-button always-allow/deny
  cards (`gate/{gate,policy,approvals}.py`; runtime mutations persist to the sqlite
  `policy`/`approvals` tables in `persistence/`). Reused everywhere.
- **M4 memory — ✅ done.** Markdown + `[[wikilinks]]`, namespaced facts files; `facts/`
  listing auto-generated at prompt-build time (no stored index); chief writes memory
  in-session via its Write tool; auto-save+notify; `Soul.md`/`User.md` persona assembly
  (`memory/{store,markdown_backend}.py`, `core/personas.py`). Every memory write is
  **git-versioned** for reversibility (`memory/versioning.py`; config `memory_git` /
  `git_author_*`; git installed in the core image).

**Phase 2 — Tools & people**
- **M5 calendar — ✅ done (scope widened).** Rather than vendor a community Google MCP,
  chief builds its **own three FastMCP servers** — calendar (`docker/mcp-calendar`, :8003),
  drive (:8001), sheets (:8002) — each a separate container behind the `google` compose
  profile, reached over streamable-HTTP at `http://mcp-<svc>:<port>/mcp` (no host ingress).
  One shared OAuth token, minted once locally (`python -m chief.tools.google.auth`), covers
  all three scopes. **Wiring (`tools/{calendar,drive,sheets}/mcp.py`): reads ALLOW
  (pre-approved), writes ASK (approval card)**; `delete-event` is deferred (hard-blocked).
  Calendar is live (`calendar_enabled: true`); **Drive + Sheets servers are built but
  disabled** (`drive_enabled`/`sheets_enabled: false`) — flipping them on, plus Gmail, is M8.
- **M6 guest mode — ✅ done.** Tier isolation by construction: a guest session is built
  with **only** its receptionist tools — the leak where guests inherited the owner's
  memory file tools + cwd is closed (`core/tasks.py` `_wire_guest_session`, `cwd=None`,
  `allowed_tools` carries no `Read/Glob/Grep`). The **3 guest tools** are `leave_message`
  (in-process relay to the Front Desk, `tools/guest.py:GuestService`) plus the **narrowed
  calendar** (`tools/calendar/mcp.guest_service` — `get-freebusy` ALLOW, `create-event`
  ASK→Front-Desk card; event details never exposed). **Notify-on-first-contact admission**
  is a dedicated, stateless Admit/Block card (`adapters/base.py`; the button payload
  carries `contact_id`+action, so taps resolve restart-proof with no re-arm). **Rate
  limits** are a fixed-window per-guest + global counter (`persistence/rate_limits.py`),
  enforced in the shared adapter gate (`base.py:handle_guest_message`: block → rate →
  mute → admit → dispatch). **Block/mute/unblock** is owner natural-language via the
  owner-only `manage_guest` tool (`GuestAdminService`). Guests run on `guest_model`
  (Sonnet, never Opus); guest transcripts are never distilled into owner memory. Opt-in
  (`guest_enabled` default off; requires `front_desk_thread_key`). DM-only — group
  @mention stays M11.
- **M7 shell + workspace + web — ✅ done.** *(Shell + workspace posture superseded by
  the 2026-07 host-native rework — see the top section; web is unchanged.)* Secret-free **sandbox** worker container
  (`docker/sandbox`, compose profile `sandbox`): read-only rootfs + tmpfs, no secrets, mem/
  PID/CPU caps, a long-lived bash per task over a narrow stdlib-asyncio TCP RPC
  (`src/chief/sandbox/shell_server.py` ← `tools/shell.py`). The owner bash tool is
  **default-ask gated** — every command routes through the approval card unless pre-blessed.
  **File workspace** = a `/workspace` volume shared core↔sandbox; the gate widens Read/Glob/
  Grep and Write/Edit to memory ∪ workspace (`gate/gate.py`).
  **Web** = the SDK built-ins WebSearch + WebFetch (GET-only), wired owner-only and un-gated
  (read-only). Shell + workspace ship **opt-in** (`shell_enabled`/`workspace_enabled` default
  off, mirroring the Google profile); web is live.
- **M8 Gmail + Drive/Sheets + media — ✅ done.** Last Phase-2 milestone, four parts.
  **Gmail R/W** (`tools/gmail/mcp.py`, compose svc behind the `google` profile): read/search/
  summarize ALLOW, **send** ASK (approval card) as the owner with a transparent signature;
  same one-token OAuth as calendar/drive/sheets. **Drive + Sheets** servers (built M5) are
  now wired config-ready — owner activates the trio by flipping `gmail_enabled`/`drive_enabled`/
  `sheets_enabled` on and re-minting the token over the widened scopes (no code change).
  **Media intake** (owner-only, ≤5 attachments, ≤20 MB each): image + PDF are downloaded by
  the adapter and threaded into the SDK turn as content blocks (`adapters/base.py:Attachment`,
  `core/session.py`); guests stay text-only by construction. **Smart output**: replies that
  overflow the per-platform limit are boundary-split (fenced code stays whole) or, when very
  long / a single code block won't fit, sent as a timestamped `.md` file
  (`split_message`/`should_send_as_file`, `core/tasks.py:_emit_final`).

**Phase 3 — Proactivity & extensibility**
- **M9 scheduler — ✅ done.** reminders + recurring + monitors, quiet hours, uptime
  heartbeat, and **usage budgeting**: each turn's SDK cost rolls into a month-to-date
  total against a fixed credit (`core/budget.py`, `persistence/usage.py`), warns the owner
  once per threshold, and on (near-)exhaustion pauses the cycle and posts a choice card
  (downgrade / continue full-quality / approve overflow) — a persisted mode, restart-proof
  (the admission-card pattern), enforced by `TaskManager`. Opt-in (`budget_enabled`).
- **M10 skills — ✅ done.** Agent-SDK skills framework: a curated local plugin
  (`vendor/chief-skills` — a hand-picked subset of `anthropics/skills` + the chief-owned
  `setup-morning-brief`, chief-drafts/owner-approves) wired onto **owner sessions only**
  via the per-session `skills=` filter (`_wire_owner_session`), the plugin path resolved
  absolute in `build_engine` so the owner session's `cwd=memory_dir` can't mis-resolve
  it. Opt-in (`skills_enabled`). The four **office-doc skills** (docx/pdf/pptx/xlsx) shell
  out to a Python + Node (docx-js/pptxgenjs) + pandoc + LibreOffice + poppler toolchain
  baked into the **M7 sandbox image** (`docker/sandbox/Dockerfile`), alongside the vendored
  skill tree at the same `/app/vendor/...` path core hands the model. So they are functional
  **only when `shell_enabled` + the `sandbox` profile are on** (they have no bash otherwise);
  the image grows ~600 MB but its security posture (secret-free, read-only, capped) is
  unchanged. `HOME=/tmp` in that image so LibreOffice has a writable profile.
- **M11 group chats — ✅ done.** A third **surface** (`Surface` enum: `HOME | DM |
  GROUP`, orthogonal to `Tier`) beyond the owner's home forum/server and 1:1 DMs. In a
  GROUP (any non-home group/supergroup or guild chief is invited to, opt-in via
  `group_chat_enabled`) chief **reads every message ambiently** — buffered per group
  (`observe` → bounded `_group_buffers`, `group_context_max_messages`), sender-attributed
  — but stays **silent until engaged** (`is_engaged`: an @mention of the bot or a reply
  to its message). An engaged **owner** runs the full owner surface flat (no topic spawn)
  with a `GROUP_MODE_NOTE` appended (public room, don't leak private context) and **every
  tool approval DM'd** to `primary_thread_key` (`_approval_route`, fail-closed); an engaged
  **non-owner** gets the receptionist (no tools) under its own `{id}:grp:guest` session
  key. One shared `{id}:grp` session/buffer per group; default off ⇒ today's behavior.
- **M11 Opus escalation — ✅ done.** The owner runs `owner_model_default` (Sonnet 4.6);
  Opus 4.8 (`owner_model_opus`) is **always owner-approved** since it burns the Max
  budget faster. Two paths: **`/opus`** pre-approves and switches the thread now (no
  card), **`/sonnet`** reverts; and — opt-in via `opus_auto_detect` — a per-turn Haiku
  complexity check (`is_complex`) **asks** before escalating a complex owner turn. An
  escalation persists on `Task.model` (the thread reopens on Opus after a restart) and
  **wins over an active budget downgrade** (the confirmation warns it spends faster). The
  auto-detect ask runs in the consumer at turn start (never inline in `dispatch` — PTB's
  sequential updates would deadlock the resolving tap) and reuses the approval card via
  the `OpusEscalation` pseudo-kind (no tool-policy rule written); a denial suppresses
  re-asking for that task. Guests are pinned to `guest_model` — never Opus.

**Phase 4 — Ship**
- **M12 Discord adapter** ✅ *(built early)***:** private server, threads = tasks; full
  parity with Telegram (commands, 4-button approval cards, guest ack), runs alongside it
  off one DB on its own platform-bound engine stack.
- **M13 hardening:** CI/CD (git-build + SSH; *deploy since removed — host-native*),
  encryption-at-rest (age), memory-repo backup,
  least-privilege, `BOOTSTRAP.md` onboarding, live sandbox tests. **Schema migrations:**
  Alembic is adopted; the schema is fully migration-driven. `init_db` runs
  `alembic upgrade head` at startup (via `_run_migrations`); `create_all` is not used
  for the real DB. Revision `0001_baseline` captures the full initial model schema. A
  drift test asserts that `upgrade head` against an empty DB produces a schema identical
  to `Base.metadata` with no autogenerate diff remaining.

  **Single Alembic tree (issue #14):** `src/chief/alembic/` is the SOLE migration tree.
  The top-level `alembic.ini` is a thin dev-CLI redirect (`script_location =
  src/chief/alembic`) so `uv run alembic ...` works from the repo root. Runtime
  `_run_migrations` and all drift tests resolve the same package copy via
  `importlib.resources` (`_ALEMBIC_INI` in `db.py`). New revisions go in
  `src/chief/alembic/versions/` only — there is no second dev-only copy.

  **Non-root core (issue #6, superseded — core no longer runs in a container):** The
  `core` container ran as **uid/gid 1000** (`chief`),
  matching the MCP services and sandbox. `Dockerfile.core` creates the user, pre-chowns
  `/data /memory /workspace /home/chief/.claude`, and sets `HOME=/home/chief`.
  `docker-compose.yml` pins `user: "1000:1000"` and `security_opt: no-new-privileges:true`.
  Docker secrets are always mounted read-only at `/run/secrets/<name>` (tmpfs, mode `0444`);
  host secret files must be `0600`. **First deploy on fresh volumes needs no extra step** —
  Docker initialises volume ownership from the image. **Existing deploys** with root-owned
  volumes need a one-time `chown -R 1000:1000` pass; the exact command is in
  `secrets/README.md`.

  **Writable paths under `read_only: true` (issue #16, superseded — host-native):**
  The `core` rootfs was read-only.
  All writes go to one of three writable mount points:

  | Path | Mount | What lives there |
  |---|---|---|
  | `/data` | `sqlite-data` volume | `chief.db` (SQLite) |
  | `/memory` | `memory` volume | markdown memory files |
  | `/workspace` | `workspace` volume | shell/workspace scratch (opt-in) |
  | `/home/chief/.claude` | `claude-home` volume | claude CLI state: `projects/` (transcripts), `.credentials.json`, `.claude.json`, `.claude.json.lock` |
  | `/tmp` | tmpfs (`uid=1000`) | Python/Node/git ephemeral scratch |

  `CLAUDE_CONFIG_DIR=/home/chief/.claude` (set in `docker-compose.yml`) is the key: when
  set, the CLI writes its top-level config (`$CLAUDE_CONFIG_DIR/.claude.json`) and lock
  (`$CLAUDE_CONFIG_DIR/.claude.json.lock`) inside the volume rather than under `$HOME`
  (which is on the read-only rootfs). Transcripts land at `$CLAUDE_CONFIG_DIR/projects/…`
  as before, so session resume is unaffected.

  **SSH deploy (superseded — host-native, 2026-07):** the VPS deploy was removed;
  chief ships as a local install (`install.sh`). Kept for the record: The `deploy` job in `.github/workflows/ci.yml`
  runs on every green push to main — `needs: [done-check, smoke-test]`. There is **no
  registry publish step**: the VPS holds a git clone of this repo (secrets/`.env`
  transferred out-of-band — they're gitignored and never travel through git or CI), and
  the job SSHes in to pull and rebuild in place:

  ```
  cd "$DEPLOY_DIR"
  git pull --ff-only
  git submodule update --init --recursive
  docker compose --profile google up -d --build
  ```

  Images are built **on the VPS itself**; `up -d --build` restarts core, triggering the
  migrate-on-start path before the app boots. Required repo settings (Settings → Secrets
  and variables → Actions):

  | Setting | Value |
  |---|---|
  | secret `VPS_HOST` | Hostname or IP of the VPS |
  | secret `VPS_USER` | SSH user (must have Docker access) |
  | secret `VPS_SSH_KEY` | Private key (OpenSSH) — public half in `~/.ssh/authorized_keys` on the VPS |
  | variable `DEPLOY_DIR` | Absolute path of the clone on the VPS; **doubles as the activation switch** — the job is skipped while it's unset (the `secrets` context isn't available in a job-level `if:`, so the guard checks `DEPLOY_DIR` instead) |
