---
name: anthropic-oauth
description: Operate chosen models on a Claude-subscription proxy — routing by alias, bare mode, dead budget cap, proxy liveness.
---

# Claude-subscription provider policy

Some of chief's LLM turns route through a **local OpenAI-compatible proxy**
(e.g. claude-code-openai-server) that drives an authenticated `claude` CLI, so
their cost is the owner's Claude **subscription**, not per-token OpenRouter
billing. This is per-model, not global: core config `provider_backends.proxy`
defines the backend, `provider_aliases` maps typed names (`opus`, `sonnet`,
`haiku` by default) onto its bare model ids, and a thread opts in with
`/model <alias>` or the `switch_model` tool. Threads left on the default alias stay on OpenRouter.
`secrets/proxy_api_key` is sent verbatim as the proxy's bearer. Nothing about
the provider seam changed — it is the same OpenAI-wire streaming path, now
fronted by the core `RouterProvider`.

## Standing constraints (do not defeat)

- **Bare mode is mandatory.** The proxy must present a plain text+tool-call
  model. If it runs non-bare it bridges tool calls to Claude's own
  Read/Edit/Bash, which collides with chief's own tools. Chief always drives
  its own tools. Symptom of a non-bare proxy: the model stops emitting tool
  calls, or edits/reads happen that chief never gated.
- **The budget cap is inert for proxied turns.** The proxy reports `cost: 0`,
  so `budget.cap_usd` cannot bound spend on aliased models — only OpenRouter
  turns are metered. Do not tell the owner a dollar cap protects proxied
  traffic. Its real limiter is the subscription's rate limits, which arrive as
  provider errors, not as budget stops.
- **The proxy is an external dependency chief does not supervise.** It is a
  separate process the owner starts and keeps alive; chief neither launches nor
  restarts it. If it is down, proxied turns fail LOUD (`error: backend
  unreachable …`) — the router never silently falls back to OpenRouter.

## Troubleshooting

- **`error: backend unreachable …` on a proxied thread** — the proxy process
  is down or `provider_backends.proxy.base_url` is wrong. Confirm the proxy is
  running and that `GET <base>/models` responds. Unrouted threads are fine.
- **401 / 403 on every proxied turn** — the bearer is wrong.
  `secrets/proxy_api_key` must equal the proxy's `CCI_API_KEY` (or any
  non-empty value if the proxy sets no bearer). Restart after fixing.
- **Auth/login errors from the proxy itself** — the underlying `claude login`
  session expired or hit a subscription rate limit. The owner re-authenticates
  the `claude` CLI on the proxy host; chief needs no change.
- **`unknown backend` at boot / model errors** — a `provider_aliases` entry
  names a backend that no longer exists, or an alias's `model` is not a bare
  name from the proxy's `/models` list (never an OpenRouter slug like
  `anthropic/…`). Fix the alias table, then restart.

## Rules

- Never write the bearer into `config.yaml` or the manifest — `proxy_api_key`
  secret only. The shared `openrouter_api_key` is untouched.
- Never claim a spend cap is enforced on proxied turns; it is not.
- Reverting is the `UNINSTALL.md` flow: remove `provider_backends.proxy` and
  its aliases, delete `secrets/proxy_api_key`, move pinned threads back to a
  default model.
