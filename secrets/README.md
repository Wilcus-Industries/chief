# Docker secrets

Drop one secret per file here (no extension, no trailing newline needed). These are
mounted at `/run/secrets/<name>` and read by `config.py`. **Never commit the real
files** — `.gitignore` keeps everything in this directory except this README out of git.

| File | Value |
|------|-------|
| `telegram_bot_token` | Bot token from @BotFather |
| `claude_code_oauth_token` | Output of `claude setup-token` (1-year Max OAuth token) |
| `google_oauth_client.json` | Google OAuth **Desktop app** client (downloaded — see below) |
| `google_calendar_token.json` | Minted by the auth helper from the client above |

Do **not** create an `ANTHROPIC_API_KEY` — it outranks the OAuth token and would bill the
API instead of the Max subscription. The app refuses to start if it is set.

## Google Calendar OAuth (M5)

`google_calendar_token.json` is a refresh token minted **once, locally** (no callback
server on the VPS — DESIGN). It is mounted into the `mcp-gcal` container. Producing it:

1. **Project** — <https://console.cloud.google.com> → create/pick a project.
2. **Enable API** — APIs & Services → Library → "Google Calendar API" → **Enable**.
3. **Consent screen** — APIs & Services → OAuth consent screen. User type **External**
   (or **Internal** with Workspace). Set app name + your email; add yourself as a
   **Test user**.
4. **Client** — Credentials → Create credentials → **OAuth client ID** → Application type
   **Desktop app** → **Download JSON**.
5. **Place it** — save that download as `secrets/google_oauth_client.json` (this file).
6. **Mint the token** — `uv sync` (pulls the host-only `google-auth-oauthlib`), then
   `python -m chief.tools.calendar.auth`. A browser opens → grant access → the token is
   written to `secrets/google_calendar_token.json`.

⚠️ **Refresh-token expiry.** An External app left in **Testing** mode expires the refresh
token after **7 days** — fatal for an always-on assistant. On the consent screen,
**Publish app** ("In production"). `calendar` is a sensitive scope, so consent shows an
"unverified app — proceed" warning; for personal single-user use that is fine and the
token stops expiring. Verification only matters for many users / removing the warning.
