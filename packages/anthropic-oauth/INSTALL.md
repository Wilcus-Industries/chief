# Installing anthropic-oauth

Points chief's LLM provider at a **local OpenAI-compatible proxy** that drives
an authenticated `claude` CLI, so turns run on the owner's Claude
**subscription over OAuth** instead of per-token OpenRouter billing. The
provider seam already speaks OpenAI wire format and already accepts a
`base_url` (core key `provider_base_url`, default OpenRouter) — this package
just flips that key and sets the model. No core code changes.

## Read before installing — the trade-offs

State these to the owner and get an explicit yes; do not install silently.

- **ToS risk.** A Claude subscription is meant for interactive use. Driving it
  behind an always-on daemon (chief's monitors, cron, channels) is heavier
  repurposing than Claude Code and carries higher account-suspension risk. The
  owner accepts this.
- **Budget cap goes inert.** `budget.cap_usd` gates spend by reading the dollar
  cost the provider reports. The proxy reports no cost, so the cap cannot bound
  spend while this is active. The real limiter becomes the subscription's own
  rate limits, which surface as provider errors.
- **The proxy must run in BARE MODEL mode.** Non-bare, the proxy bridges tool
  calls to Claude's *own* Read/Edit/Bash and collides with chief's toolset.
  Chief must drive its own tools; the model stays a plain text+tool-call
  endpoint. Verify tool calling still works before trusting it (step 6).
- **The proxy is a separate process the owner runs and keeps alive.** It is not
  installed, supervised, or updated by chief. If it dies, every turn fails.

## Steps

Gather parameters (steps 1–3), run the deterministic install once (step 4),
then verify (steps 5–6).

1. **Confirm the owner has a working proxy.** Recommended:
   `github.com/schmarta/claude-code-openai-server`. It requires a prior
   `claude login` on the host (the subscription auth it reuses) and must be
   started in **bare model mode** exposing `POST /v1/chat/completions`. Ask the
   owner for its base URL (e.g. `http://127.0.0.1:8000/v1`) and confirm it is
   up: a `GET <base>/models` should list at least one model.
2. Ask the owner which **bare model name** the proxy exposes (from that
   `/models` list, e.g. `claude-sonnet-4.5`) — this becomes `models.default`.
3. Set the **bearer** the proxy expects. The provider sends
   `secrets/openrouter_api_key` verbatim as `Authorization: Bearer …`. If the
   proxy sets `CCI_API_KEY`, write that value to `secrets/openrouter_api_key`
   (overwriting the OpenRouter key — save the old one first if the owner may
   revert). If the proxy has no bearer, any non-empty placeholder works. Never
   put the key in `config.yaml` or the manifest.
4. Place the skill and set config, deterministically:
   - Run `PROXY_URL="<base url>" MODEL="<bare model>" bash
     packages/anthropic-oauth/install.sh` via Bash. It copies the skill verbatim
     and sets `provider_base_url` + `models.default` (via `chief.config_apply`).
     Or do the same by hand with your file tools.
   - Record the install in `data/installed.yaml` (`anthropic-oauth`,
     `source: bundled`).
   - `restart` — one guarded commit brings the skill + config live and rebuilds
     the provider against the proxy.
5. **Verify a turn runs.** After restart, send a normal message and confirm a
   reply streams back. A connection error means the proxy is down or the base
   URL is wrong; a 401/403 means the bearer is wrong (step 3).
6. **Verify tool calling.** Ask chief to do something that needs a tool (e.g.
   read a file). If the model never emits tool calls, the proxy is not in bare
   mode — stop and fix the proxy before relying on this. See the
   `anthropic-oauth` skill for ongoing operation and troubleshooting.
