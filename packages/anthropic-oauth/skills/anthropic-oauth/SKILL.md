---
name: anthropic-oauth
description: Operate chief on a Claude-subscription proxy — bare mode, dead budget cap, proxy liveness.
---

# Claude-subscription provider policy

Chief's LLM turns run through a **local OpenAI-compatible proxy** (e.g.
claude-code-openai-server) that drives an authenticated `claude` CLI, so cost
is the owner's Claude **subscription**, not per-token OpenRouter billing. Core
config `provider_base_url` points at the proxy; `models.default` is the bare
model name the proxy exposes; `secrets/openrouter_api_key` is sent verbatim as
the bearer. Nothing about the provider seam changed — it is the same
OpenAI-wire streaming path OpenRouter used.

## Standing constraints (do not defeat)

- **Bare mode is mandatory.** The proxy must present a plain text+tool-call
  model. If it runs non-bare it bridges tool calls to Claude's own
  Read/Edit/Bash, which collides with chief's own tools. Chief always drives
  its own tools. Symptom of a non-bare proxy: the model stops emitting tool
  calls, or edits/reads happen that chief never gated.
- **The budget cap is inert.** The proxy reports `cost: 0`, so `budget.cap_usd`
  cannot bound spend. Do not tell the owner a dollar cap is protecting them
  while this package is active. The real limiter is the subscription's rate
  limits, which arrive as provider errors, not as budget stops.
- **The proxy is an external dependency chief does not supervise.** It is a
  separate process the owner starts and keeps alive; chief neither launches nor
  restarts it. If it is down, every turn fails.

## Troubleshooting

- **Connection refused / timeouts on every turn** — the proxy process is down
  or `provider_base_url` is wrong. Confirm the proxy is running and that
  `GET <base>/models` responds.
- **401 / 403 on every turn** — the bearer is wrong.
  `secrets/openrouter_api_key` must equal the proxy's `CCI_API_KEY` (or any
  non-empty value if the proxy sets no bearer). Restart after fixing.
- **Auth/login errors from the proxy itself** — the underlying `claude login`
  session expired or hit a subscription rate limit. The owner re-authenticates
  the `claude` CLI on the proxy host; chief needs no change.
- **Model errors / unknown model** — `models.default` must be a bare name from
  the proxy's `/models` list, not an OpenRouter slug like `anthropic/…`.

## Rules

- Never write the bearer into `config.yaml` or the manifest — secrets only.
- Never claim a spend cap is enforced while this is active; it is not.
- Reverting to OpenRouter is the `UNINSTALL.md` flow: reset
  `provider_base_url`, `models.default`, and the OpenRouter key in
  `secrets/openrouter_api_key`.
