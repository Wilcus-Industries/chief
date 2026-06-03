# Docker secrets

Drop one secret per file here (no extension, no trailing newline needed). These are
mounted at `/run/secrets/<name>` and read by `config.py`. **Never commit the real
files** — `.gitignore` keeps everything in this directory except this README out of git.

| File | Value |
|------|-------|
| `telegram_bot_token` | Bot token from @BotFather |
| `claude_code_oauth_token` | Output of `claude setup-token` (1-year Max OAuth token) |

Do **not** create an `ANTHROPIC_API_KEY` — it outranks the OAuth token and would bill the
API instead of the Max subscription. The app refuses to start if it is set.
