# Docker secrets

Drop one secret per file here (no extension, no trailing newline needed). These are
mounted at `/run/secrets/<name>` and read by `config.py`. **Never commit the real
files** — `.gitignore` keeps everything in this directory except this README out of git.

## Host file permissions

Secret files **must be owner-readable only** (`0600`) on the host. Docker reads them as
root when building the secret tmpfs, but keeping them `0600` prevents other host
processes or users from reading them:

```sh
chmod 0600 secrets/telegram_bot_token secrets/discord_bot_token \
           secrets/claude_code_oauth_token secrets/google_oauth_client.json \
           secrets/google_token.json
```

## Secret mounts are read-only

Docker mounts each secret as a tmpfs file at `/run/secrets/<name>` with mode `0444`
(read-only) inside the container. This is enforced by Docker — no extra `:ro` flag is
needed and it cannot be overridden from the container process.

## One-time volume chown (pre-existing installs)

The core container now runs as **uid/gid 1000** (`chief`). On a **fresh deploy** Docker
initialises the named volumes from the image's pre-chowned directories, so no manual step
is needed.

On an **existing deploy** where the `sqlite-data`, `memory`, `workspace`, or `claude-home`
volumes were created when core ran as root, their contents are still owned by `uid 0`.
Run this once before the first non-root `compose up`:

```sh
# Re-own all four volumes in one shot using a throwaway busybox container.
docker run --rm \
  -v chief_sqlite-data:/data \
  -v chief_memory:/memory \
  -v chief_workspace:/workspace \
  -v chief_claude-home:/home/chief/.claude \
  busybox \
  chown -R 1000:1000 /data /memory /workspace /home/chief/.claude
```

Adjust the volume name prefix (`chief_`) if your `docker compose` project name differs
(check with `docker volume ls | grep chief`). After this, `docker compose up -d core`
will start cleanly as uid 1000.

| File | Value |
|------|-------|
| `telegram_bot_token` | Bot token from @BotFather |
| `discord_bot_token` | Bot token from the Discord Developer Portal (Bot → Reset Token) |
| `claude_code_oauth_token` | Output of `claude setup-token` (1-year Max OAuth token) |
| `google_oauth_client.json` | Google OAuth **Desktop app** client (downloaded — see below) |
| `google_token.json` | Minted by the auth helper — one token, Calendar + Drive + Sheets + Gmail |

## Chat platforms (Telegram and/or Discord)

Configure **either or both** — the app refuses to boot with neither. Each platform needs
its token (above) **and** its owner id (set as an env var, not a secret):

| Env var | Value |
|---------|-------|
| `OWNER_TELEGRAM_ID` | Your numeric Telegram user id (ask @userinfobot) |
| `OWNER_DISCORD_ID` | Your numeric Discord user id (Developer Mode → right-click yourself → Copy User ID) |

Docker requires every referenced secret file to exist, so for a platform you skip, create
an empty token file (e.g. `touch secrets/discord_bot_token`). The Discord bot also needs
the privileged **message_content** intent — enable it under Bot → Privileged Gateway
Intents in the Developer Portal, or Discord delivers empty message content.

Do **not** create an `ANTHROPIC_API_KEY` — it outranks the OAuth token and would bill the
API instead of the Max subscription. The app refuses to start if it is set.

## Google OAuth (M5/M8) — Calendar + Drive + Sheets + Gmail

`google_token.json` is **one** refresh token minted **once, locally** (no callback server
on the VPS — DESIGN), covering all four scopes (`calendar`, `drive`, `spreadsheets`,
`gmail.modify`). It is bind-mounted into every Google MCP container — `mcp-calendar`,
`mcp-drive`, `mcp-sheets`, `mcp-gmail` (the `google` compose profile). Producing it:

1. **Project** — <https://console.cloud.google.com> → create/pick a project.
2. **Enable APIs** — APIs & Services → Library → **Enable** each of: "Google Calendar
   API", "Google Drive API", "Google Sheets API", "Gmail API".
3. **Consent screen** — APIs & Services → OAuth consent screen. User type **External**
   (or **Internal** with Workspace). Set app name + your email; add yourself as a
   **Test user**.
4. **Client** — Credentials → Create credentials → **OAuth client ID** → Application type
   **Desktop app** → **Download JSON**.
5. **Place it** — save that download as `secrets/google_oauth_client.json` (this file).
6. **Mint the token** — `uv sync` (pulls the host-only `google-auth-oauthlib`), then
   `python -m chief.tools.google.auth`. A browser opens → grant access to all three
   scopes → the token is written to `secrets/google_token.json`.

The bind-mount must stay writable for the **mcp-sheets** container (the sole writer — it
persists the refreshed token; calendar + drive refresh in memory only, and **mcp-gmail**
mounts it read-only and refreshes into a throwaway `/tmp` copy it seeds at startup). Run
the containers as the host owner of the file: `MCP_GOOGLE_UID`/`MCP_GOOGLE_GID` default to
`1000`, override if you aren't uid 1000.

**Adding Gmail/Drive/Sheets to an existing deploy (M8).** If you minted the token before
M8 (calendar scope only) or before Drive/Sheets were enabled, **re-mint** it so the new
scopes are granted: flip `drive_enabled`/`sheets_enabled`/`gmail_enabled` in `config.yaml`,
re-run `python -m chief.tools.google.auth` (re-consent, now including Gmail), then
`docker compose --profile google up -d`. The signature on outbound mail comes from
`GMAIL_SIGNATURE` (env, optional) with `{owner}` filled from `OWNER_NAME`.

⚠️ **Refresh-token expiry.** An External app left in **Testing** mode expires the refresh
token after **7 days** — fatal for an always-on assistant. On the consent screen,
**Publish app** ("In production"). `calendar`/`drive`/`spreadsheets` are sensitive scopes,
so consent shows an "unverified app — proceed" warning; for personal single-user use that
is fine and the token stops expiring. Verification only matters for many users / removing
the warning.
