# S0 — Walking skeleton (throwaway)

Day-1 de-risking slice from `DESIGN.md`. Two independent proofs:

- **S0a — Max auth.** Does `CLAUDE_CODE_OAUTH_TOKEN` run the Agent SDK headless on the
  **subscription credit** (not the API)? This is the existential unknown. **No Telegram.**
- **S0b — Telegram round-trip.** Owner DM → SDK → reply, over long-poll.

All of this is disposable; M0 replaces it and deletes `s0/`.

> The SDK shells out to the `claude` CLI, so the Docker image installs Node + the Claude
> Code CLI. Running locally needs the `claude` CLI on your `PATH` too.

## 0. One-time: mint the OAuth token

Interactive browser login — run it yourself (in this session, type the `!`-prefixed form):

```
! claude setup-token
```

Copy the token. Then:

```
cp s0/.env.example s0/.env      # fill in CLAUDE_CODE_OAUTH_TOKEN (and TG vars for S0b)
```

Make sure `ANTHROPIC_API_KEY` is **unset** in your shell — it outranks the OAuth token and
would bill the API. The scripts hard-fail if they see it.

## 1. S0a — auth proof

**Local (fastest):**

```
unset ANTHROPIC_API_KEY
export CLAUDE_CODE_OAUTH_TOKEN=...      # or: set -a; . s0/.env; set +a
uv run python s0/verify_auth.py
```

**In Docker (the real headless proof):**

```
docker compose --env-file s0/.env -f s0/compose.yml run --rm auth-proof
```

**Success looks like:** prints `assistant reply: 'pong'`, a non-null `total_cost_usd`,
`model_usage`, and a `session_id`, ending with `S0a OK`. Then **cross-check the Claude
Console usage view**: the call should draw the **Agent SDK subscription credit, not API
spend**. That cross-check is what truly retires verify-#1.

## 2. S0b — Telegram round-trip

Fill `TELEGRAM_BOT_TOKEN` (from @BotFather) and `OWNER_TELEGRAM_ID` (your numeric id, via
@userinfobot) in `s0/.env`, then:

```
docker compose --env-file s0/.env -f s0/compose.yml up bot
```

DM your bot from the owner account. It replies with the model's output. Messages from
anyone else are ignored (S0 has no tiers/topics/gate — those start at M0).

## Notes

- **Throwaway scope:** the project done-check (`pytest`/`mypy`) is waived here; the
  deliverable is a verified proof, not production code. `ruff check .` is kept clean.
- **Secrets:** S0 passes tokens via `.env`; real Docker secrets arrive at M0.
- When both proofs pass, mark **S0** done in `DESIGN.md`'s build plan.
