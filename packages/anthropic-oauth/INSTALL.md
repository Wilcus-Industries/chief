# Installing anthropic-oauth

Adds a named **proxy** backend and one or more typed **aliases** so that
chosen models route to a **local OpenAI-compatible proxy** driving an
authenticated `claude` CLI — turns on those models run on the owner's Claude
**subscription over OAuth** instead of per-token OpenRouter billing. Everything
else stays on the default backend (OpenRouter). Nothing is global: a thread
opts in with `/model <alias>` or the agent's `switch_model` tool; unrouted
threads never touch the proxy. The provider seam already speaks OpenAI wire and
already routes by model name (core `RouterProvider`) — this package just fills
in `provider_backends` + `provider_aliases`. No core code changes.

## Read before installing — the trade-offs

State these to the owner and get an explicit yes; do not install silently.

- **ToS risk.** A Claude subscription is meant for interactive use. Driving it
  behind an always-on daemon (chief's monitors, cron, channels) is heavier
  repurposing than Claude Code and carries higher account-suspension risk. The
  owner accepts this.
- **Budget cap goes inert for proxied turns.** `budget.cap_usd` gates spend by
  reading the dollar cost the provider reports. The proxy reports no cost, so
  spend on aliased models is unmetered — only OpenRouter turns count against
  the cap. The real limiter on the subscription is its own rate limits, which
  surface as provider errors.
- **The proxy must run in BARE MODEL mode.** Non-bare, the proxy bridges tool
  calls to Claude's *own* Read/Edit/Bash and collides with chief's toolset.
  Chief must drive its own tools; the model stays a plain text+tool-call
  endpoint. Verify tool calling still works before trusting it (step 6).
- **The proxy is a separate process the owner runs and keeps alive.** It is not
  installed, supervised, or updated by chief. If it dies, every proxied turn
  fails LOUD (`error: backend unreachable …`) — the router never silently
  falls back to OpenRouter.

## Steps

Gather parameters (steps 1–3), run the deterministic install
(step 4), then verify (steps 5–6).

1. **Confirm the owner has a working proxy.** Recommended:
   `github.com/schmarta/claude-code-openai-server`. It requires a prior
   `claude login` on the host (the subscription auth it reuses) and must be
   started in **bare model mode** exposing `POST /v1/chat/completions`. Ask the
   owner for its base URL (e.g. `http://127.0.0.1:8000/v1`) and confirm it is
   up: a `GET <base>/models` should list at least one model.
2. **Model ids and aliases.** The install wires the three standard aliases —
   `opus`, `sonnet`, `haiku` — to matching pinned model ids by default, so the
   owner need not choose anything. Only ask if they want something else: a
   model outside that set, or a different alias name. The alias is the name
   the owner types after `/model`. Every id must appear in the proxy's
   `/models` list; the script checks and refuses otherwise, because some
   proxies echo an unknown id back on a completion instead of erroring.
3. Set the **bearer** the proxy expects in the package's **own** secret,
   `secrets/proxy_api_key` (no longer the shared `openrouter_api_key`, so
   OpenRouter keeps working for unrouted threads). The proxy backend sends this
   verbatim as `Authorization: Bearer …`. If the proxy sets `CCI_API_KEY`,
   write that value there; if it has no bearer, any non-empty placeholder
   works. Never put the key in `config.yaml` or the manifest.
4. Place the skill and set config, deterministically:
   - Run `PROXY_URL="<base url>" bash packages/anthropic-oauth/install.sh` via
     Bash — that installs the standard `opus`/`sonnet`/`haiku` set. For a
     model outside it, add `MODEL="<bare model id>" ALIAS="<alias>"` to
     install exactly that one (both together, or the script refuses). It
     **fails fast before touching anything** if `secrets/proxy_api_key` is
     missing/empty (step 3), the proxy doesn't answer `GET <base>/models`
     (step 1), or an id isn't in that list — a dead endpoint or unserved
     model is never wired into config. Then it copies the skill verbatim and
     adds `provider_backends.proxy` + every `provider_aliases.<alias>` in one
     `chief.config_apply` call (all or nothing), and records the install in
     `data/installed.yaml` (via `chief.registry_apply`). Re-running adds
     aliases without clobbering existing ones — the config deep-merges.
     Run the script; do not re-create its steps by hand.
   - `restart` — the guarded commit brings the skill live; the config lands by
     disk reload (gitignored, not rolled back on failure) and rebuilds
     the router with the new proxy backend and aliases.
5. **Verify a proxied turn runs.** After restart, in some thread send
   `/model <alias>` then a normal message and confirm a reply streams back. A
   connection error (`error: backend unreachable …`) means the proxy is down or
   the base URL is wrong; a 401/403 means the bearer in `secrets/proxy_api_key`
   is wrong (step 3). Threads left on the default alias keep using OpenRouter.
6. **Verify tool calling.** On a proxied thread, ask chief to do something that
   needs a tool (e.g. read a file). If the model never emits tool calls, the
   proxy is not in bare mode — stop and fix the proxy before relying on this.
   See the `anthropic-oauth` skill for ongoing operation and troubleshooting.
