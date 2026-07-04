# Secrets

Drop one secret per file here (no extension, no trailing newline needed). The
host-native core reads this directory directly (`app.load_settings` — a
`~/.config/chief/secrets` dir or the `CHIEF_SECRETS_DIR` env var work too, and plain
env vars of the same name always win). **Never commit the real files** — `.gitignore`
keeps everything in this directory except this README and the `google_tokens/`
subdirectory placeholder out of git.

## Directory layout

```
secrets/
├── README.md                          ← this file (tracked)
├── claude_code_oauth_token            ← ignored, never commit
├── discord_bot_token                  ← ignored, never commit
├── google_oauth_client.json           ← ignored, never commit
├── telegram_bot_token                 ← ignored, never commit
└── google_tokens/                     ← dedicated Google account tokens subdir
    ├── .gitkeep                       ← tracked (keeps the dir in git)
    ├── google_token.json              ← ignored, never commit (primary account)
    └── google_token_<label>.json      ← ignored, never commit (additional accounts)
```

Google account tokens live exclusively in `google_tokens/` — **not** directly under
`secrets/`.  The host-native core scans it directly, and the four Google MCP
containers (`mcp-calendar`, `mcp-drive`, `mcp-sheets`, `mcp-gmail`) bind-mount this
single subdirectory (issue #58), which keeps `claude_code_oauth_token`,
`discord_bot_token`, `google_oauth_client.json`, and `telegram_bot_token` out of those
containers (issue #57).

## File permissions

Secret files **must be owner-readable only** (`0600`) so other host processes or users
cannot read them:

```sh
chmod 0600 secrets/claude_code_oauth_token secrets/discord_bot_token \
           secrets/google_oauth_client.json secrets/telegram_bot_token \
           secrets/google_tokens/google_token.json
```

| File | Value |
|------|-------|
| `telegram_bot_token` | Bot token from @BotFather |
| `discord_bot_token` | Bot token from the Discord Developer Portal (Bot → Reset Token) |
| `claude_code_oauth_token` | Output of `claude setup-token` (1-year Max OAuth token) |
| `google_oauth_client.json` | Google OAuth **Desktop app** client (downloaded — see below) |
| `google_tokens/google_token.json` | Minted by the auth helper — one token, Calendar + Drive + Sheets + Gmail |

## Chat platforms (Telegram and/or Discord)

Configure **either or both** — the app refuses to boot with neither. Each platform needs
its token (above) **and** its owner id (set as an env var, not a secret):

| Setting (config.yaml or env var) | Value |
|---------|-------|
| `owner_telegram_id` / `OWNER_TELEGRAM_ID` | Your numeric Telegram user id (ask @userinfobot) |
| `owner_discord_id` / `OWNER_DISCORD_ID` | Your numeric Discord user id (Developer Mode → right-click yourself → Copy User ID) |

For a platform you skip, simply leave its token file absent. The Discord bot also needs
the privileged **message_content** intent — enable it under Bot → Privileged Gateway
Intents in the Developer Portal, or Discord delivers empty message content.

Do **not** create an `ANTHROPIC_API_KEY` — it outranks the OAuth token and would bill the
API instead of the Max subscription. The app refuses to start if it is set.

## Google OAuth (M5/M8) — Calendar + Drive + Sheets + Gmail

`google_tokens/google_token.json` is **one** refresh token minted **once, locally**,
covering all four scopes (`calendar`, `drive`, `spreadsheets`, `gmail.modify`). The
host-native core scans `secrets/google_tokens/` directly, and the directory is
bind-mounted into the four Google MCP containers — `mcp-calendar`, `mcp-drive`,
`mcp-sheets`, `mcp-gmail` (the `google` compose profile). This is the **single
canonical host path** (issue #58): no two different paths, no manual copy step.
Additional per-account tokens (`google_token_<label>.json`) can be added there without
changing the compose file.

Producing the token:

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
   `python -m chief.tools.google.auth`. A browser opens → grant access to all four
   scopes → the token is written directly to `secrets/google_tokens/google_token.json`.
   No move or copy step is needed.

The bind-mount must stay writable for the **mcp-sheets** container (the sole writer — it
persists the refreshed token; core and the calendar/drive containers refresh in memory
only, and **mcp-gmail** seeds a throwaway `/tmp` copy at startup). Run the containers as
the host owner of the file: `MCP_GOOGLE_UID`/`MCP_GOOGLE_GID` default to `1000`, override
if you aren't uid 1000.

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
