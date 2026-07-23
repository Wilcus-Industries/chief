# DESIGN.md — chief, recoded

Target architecture for the greenfield rewrite ("chief-recode"). This replaces the
previous S0→M13 design entirely; the retired plan lives in git history.

## Vision

One general agent harness on an always-on box — Linux or macOS, no platform assumed.
Input channels are dumb pipes; capabilities are installable packages; the agent
configures — and *builds* — its own integrations. Nothing channel-specific is
hardcoded into the core. When the agent needs to watch a channel, it puts a monitor
on it; when it needs a new channel, it builds an adapter from a package skill.

macOS boxes (e.g. an always-on Mac mini) get a few extra defaults —
the iMessage adapter and the screening package it depends on; everything else Apple
comes as packages. Linux gets the same core, just without the macOS defaults.

## Principles

1. **Minimal core.** The core owns: the agent loop, sessions, the event bus,
   monitors/cron, the gate, budget, audit, persistence, and a small adapter
   interface. Everything else — including memory — is a package.
2. **Channels are dumb pipes.** An adapter delivers inbound `Message`s and exposes
   `send`. No per-channel features in core — no watches, no admission state machines,
   no group-chat *policy*. An adapter reports a group message's conversation as its
   `thread_key` (identity, not a branch); which groups matter is a monitor.
3. **Behavior is policy, safety is code.** The tool gate (never/approve lists,
   approval cards) is code-enforced. Everything behavioral — how to treat strangers,
   group etiquette, when to escalate — is prompt/policy the agent applies.
4. **The agent edits itself.** Config, prompts, skills, and its own source — through a
   guarded pipeline (branch → done-check green → restart → healthcheck → rollback).
5. **Design clean for eventual publicity.** No personal data hardcoded, secrets in
   env/files, generic naming. Docs/packaging investment comes later.
6. **Small files.** <200 LOC hard cap per production file, <100 ideal (STYLEGUIDE,
   CI-enforced).

## Language & stack

- **Python** (≥3.12, uv). The daemon is IO-bound; self-editing + agent-written plugins
  demand a dynamic language, and LLMs write Python best. Compiled alternatives (Go,
  Nim, Rust) were rejected: the edit→recompile→restart cycle and weak LLM fluency
  fight the self-edit vision.
- Same repo, replaces `src/chief` immediately. Name stays **chief**. Keep the
  installer story, STYLEGUIDE, and toolchain (`uv run pytest`, `ruff`, `mypy`).
- Fresh SQLite schema (no Alembic baggage carried over). Old tasks/contacts/watches
  start empty. The existing markdown memory dir imports into the memory package
  (below) once one is installed.

## Core

### Own harness (no vendor SDK)

The agent loop is ours, built on a provider seam:

- **LLM provider adapter** — one common interface (chat + tool calls + streaming);
  OpenRouter is the first implementation. OpenAI, Anthropic, Google, Groq, etc. port
  in later. All model calls, including cheap judgments, go through it.
- **Tool loop + streaming** — model call → tool dispatch → results → loop until done;
  deltas stream out to the session's adapter.
- **Session persistence/resume** — conversation state survives daemon restart.
- **Context compaction** — auto-summarize long threads to stay in window.
- **MCP client (HTTP + stdio)** — speaks MCP to external servers: HTTP for remote
  servers, stdio for the wider ecosystem (the daemon spawns and supervises server
  child processes — lifecycle, restart on crash).
  In-process tools register through a small native tool interface.

### Sessions

Per-thread sessions, like today: each conversation thread gets its own session and a
serial turn queue; N threads run concurrently under a semaphore. Monitors wake their
own thread or spawn a fresh one.

### Model configuration

No preset roles. Config ships with exactly one key: `default: <model>`. The agent
extends it on its own — inventing roles ("compaction", "cheap-judgment", whatever it
finds useful) via self-config. It can switch its own model per task via a tool ("this
needs a bigger model"); the owner can override per-thread. The budget cap is set
during bootstrap on every platform — runaway spend is bounded before the agent makes
its first call.

### Skills, subagents, commands

- **Skills** use the public Anthropic SKILL.md convention (dir with SKILL.md
  frontmatter + body, optional scripts/resources) — models already know it, and
  existing open skills drop into chief-packages unmodified. Loading is **progressive
  disclosure**: the system prompt carries name + description one-liners only; the
  agent pulls a skill's full body via tool call when relevant.
- **Subagents: full registry.** Named agent definitions (custom system prompt, tool
  allowlist, optional model) live in the harness dir; packages can ship agents; the
  agent can author its own (self-edit pipeline applies). A spawn tool runs a
  sub-session with its own tool loop and returns the final result to the parent.
- **Classifiers: internal categorical-label primitive.** Named definitions
  (frontmatter: name, description, labels, optional model; body: the classification
  prompt) live in the classifiers dir; the agent self-authors them via the self-edit
  pipeline exactly like subagents. A `classify` call maps text to exactly one declared
  label — case/whitespace tolerant, retrying twice more before raising. The model
  resolves frontmatter → the `default_classifier` role → the default model. There is
  **no** agent-facing tool: it is an internal seam core services (e.g. monitors) call.
- **Owner commands.** A tiny core slash-command set (/tasks, /cancel, /model, …) is
  parsed before agent dispatch on every adapter — a deterministic escape hatch when a
  session is wedged or burning money. /skill-name invokes a skill directly; natural
  language works everywhere too.
- **MCP servers are config.** There is no add-server tool: the agent wires in a new
  server (url or command) by self-editing the `mcp_servers` config key, and the manager
  connects it on the next boot — the same seatbelt and audit trail as any self-edit.

### Events + monitors + cron

- Adapters publish every inbound event to an **event bus**.
- A **monitor** is a subscription with a predicate that wakes the agent when it
  fires. Its tool exposes three forms: a **code/regex** match (cheap), an
  **instruction** (a yes/no judgment via the built-in wake-judge classifier), and a
  named **classifier** + fire label (fires when that classifier returns the label).
- **Cron/interval** schedules remain for time-based work (recurring errands,
  reminders, web checks), with quiet-hours deferral.
- The old "watches" feature is gone; the agent creates monitors on channels itself.

### Gate, budget, audit

- **Gate:** thin, code-enforced. NEVER/APPROVED lists, approval cards for the gray
  zone. Cards render on the surface the session came from (web buttons, iMessage
  plain-text yes/no, CLI frames); first answer wins.
- **Budget:** OpenRouter dollars per cycle; warn/exhaust thresholds; downgrade-model
  behavior on exhaustion.
- **Audit:** append-only JSONL of every tool call and gate decision. Non-negotiable
  for debugging a self-editing agent.

### Strangers

Unknown senders are logged (metadata only), never invoke the agent, never get a
reply. Visible in the web UI. Guest interaction is a later package/policy, not core.

## Adapters

Core owns a small adapter interface: deliver inbound `Message(channel, sender,
thread_key, text, attachments)`, expose `send(thread_key, text/file)`, publish to the
event bus. Reference adapters shipped in core:

- **Web UI** — minimal rebuild: chat, approval cards, monitor list. Server-rendered
  htmx + SSE, owner-password auth, in-process ASGI. Cockpit extras come later.
- **CLI/socket** — local control plane, always on.
- **iMessage** — shipped in core but **macOS-only**; its notify policy is configured
  by the BUILD-IMESSAGE package skill (below).

Every other channel (Discord, Telegram, Slack, …) is agent-built via a BUILD-\*
package.

## Packages

A separate public repo (**chief-packages**) the agent clones/pulls. Core ships the
pointer plus a few bundled defaults.

**Package =** a directory with:

- `manifest` — name, MCP servers to register, skills to link, config keys, secrets
  needed, **dependencies** (other packages).
- `INSTALL.md` — steps the agent follows for the messy real-world parts (the
  OAuth dance, brew install, macOS permission prompts).

**Two kinds:**

1. **Install packages** — wire up existing capability (Google MCP servers, browser,
   push notifications, injection screening, memory systems).
2. **BUILD-\* skills** — guide the agent to *write real code* (e.g. a new channel
   adapter) into the harness dir, loaded only after the guarded pipeline passes.

**Dependency trees:** manifests declare dependencies; the **screening package**
(cheap-model injection screen on untrusted content) is a dependency of every
public-facing package — including iMessage, so it installs by default on macOS.

### Memory as a package

Core ships no memory system. A memory package brings its own tools (recall/save) and
prompt/skill instructions via the manifest — no special seam in core. First package:
today's markdown-files-plus-git store (the existing memory dir imports straight into
it). Alternatives (vector stores, DB-backed, hosted) can ship as competing packages;
the owner picks one at onboarding.

### BUILD-IMESSAGE (the archetype)

Helps the agent build/configure its own iMessage system. By default chief texts
only the owner — a third party's message is logged, never auto-answered, and no
monitor is created at install. When the owner asks chief to engage a specific
person or group, the skill has it build **one monitor scoped to that chat** (a
code predicate on the sender/thread, or an optional cheap-model "worth waking?"
screen), ideally in a fresh dedicated session so untrusted third-party context
stays isolated from the owner's self-chat.

### Agent-built code trust

Built plugins must pass tests the skill also had the agent write; the daemon loads
them (or restarts into them) only after the done-check passes; a failed healthcheck
auto-rolls back. Same seatbelt as source self-edit.

## Self-edit

The agent edits its own config, system prompt, skills, and source code. It reads
first with the read-only `read_file` / `grep` tools (package discovery is the
`chief-pkg` CLI run through the `shell` tool), then writes with `write_file` /
`edit_file`. Writes to source are inert until a `restart`: the change goes branch
→ done-check green → restart into new code → healthcheck → auto-rollback on
failure (`revert_edits` backs a bad edit out before it ships). No human review
required; the audit log records everything.

The default native tool set is deliberately small — `read_file`, `grep`,
`write_file`, `edit_file`, `shell`, `restart`, `load_skill`, `session`,
`switch_model`, `monitor`, `schedule`, `spawn_agent` — with the
create/list/delete verbs folded into one action argument on
`session`/`monitor`/`schedule`. There is no `self_edit` or `install_package`
tool: writing is `write_file`/`edit_file`, and package install is
document-driven through `shell`. Everything channel- or capability-specific
arrives as package-registered tools.

## Bootstrap & onboarding

`install.sh` brings up the daemon + web UI (always works, any platform). The first
conversation *is* onboarding: the agent introduces itself, offers packages, and when
the owner says "set up iMessage" it runs BUILD-IMESSAGE — walking the Full Disk
Access / Automation permission steps interactively (chief texts only the owner by
default; engaging anyone else is a later, owner-driven ask).

## v1 acceptance (the demo that retires old chief)

Fresh install on a Mac → onboard via web UI → agent sets up iMessage → owner
texts it and it answers → owner asks for a monitor on a thread and a recurring
errand, both fire correctly → agent installs the Google package and reads the
calendar.

## What dies (vs. the old tree)

- Per-platform engine stacks and their near-twin builders (`app.py`).
- Telegram/Discord adapters in core (return later as BUILD packages if wanted).
- Watches + watch candidates (become agent-created monitors).
- Group-chat behavior branches, guest admission state machine, receptionist tools
  (behavioral policy or later packages). Core carries a group's chat identity as
  `thread_key` and nothing more — no membership tracking, no group etiquette.
- The Copilot SDK backend, premium-request budgeting, category routing table
  (replaced by provider seam + agent model switching).
- The ~120-field config surface (core keeps a small set; packages bring their own
  keys).

## Process

Design doc (this) → PRD via `/to-prd` → slices via `/to-issues` → build via
`/orchestrate`. TDD throughout; unit/contract tests in CI from commit one; live
e2e suites on a live macOS box (channel builds, package installs) run on-demand,
not CI-blocking.
