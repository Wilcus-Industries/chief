# Docker secrets

Drop one secret per file here (no extension, no trailing newline needed). These are
mounted at `/run/secrets/<name>` and read by `config.py`. **Never commit the real
files** — `.gitignore` keeps everything in this directory except this README and the
`google_tokens/` subdirectory placeholder out of git.

## Directory layout

```
secrets/
├── README.md                          ← this file (tracked)
├── claude_code_oauth_token            ← ignored, never commit
├── discord_bot_token                  ← ignored, never commit
├── google_oauth_client.json           ← ignored, never commit
├── telegram_bot_token                 ← ignored, never commit
├── openrouter_api_key                 ← ignored, never commit (optional — #90)
└── google_tokens/                     ← dedicated Google account tokens subdir
    ├── .gitkeep                       ← tracked (keeps the dir in git)
    ├── google_token.json              ← ignored, never commit (primary account)
    └── google_token_<label>.json      ← ignored, never commit (additional accounts)
```

**Not here: the GitHub Copilot token.** `agent_backend: copilot` (#76/#90, part of #72)
authenticates via the `copilot` CLI's own login, which is CLI-managed at
`~/.copilot/config.json` and auto-refreshes on its own. Never copy that token into this
directory — it isn't a Docker secret and doesn't follow this file's pattern.

Google account tokens live exclusively in `google_tokens/` — **not** directly under
`secrets/`.  All five Google-consuming services (`core`, `mcp-calendar`, `mcp-drive`,
`mcp-sheets`, `mcp-gmail`) source their token from this single subdirectory (issue #58),
which lets each container mount only the tokens it needs and keeps
`claude_code_oauth_token`, `discord_bot_token`, `google_oauth_client.json`, and
`telegram_bot_token` out of those containers (issue #57).

## Host file permissions

Secret files **must be owner-readable only** (`0600`) on the host. Docker reads them as
root when building the secret tmpfs, but keeping them `0600` prevents other host
processes or users from reading them:

```sh
chmod 0600 secrets/claude_code_oauth_token secrets/discord_bot_token \
           secrets/google_oauth_client.json secrets/telegram_bot_token \
           secrets/google_tokens/google_token.json secrets/openrouter_api_key
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
| `google_tokens/google_token.json` | Minted by the auth helper — one token, Calendar + Drive + Sheets + Gmail |
| `openrouter_api_key` | OpenRouter API key (optional — see "OpenRouter BYOK" below) |

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

## OpenRouter BYOK (issue #90, part of #72)

`openrouter_api_key` is the key for the `openrouter` provider target class: a session
spawned on it runs through `CopilotBackend` (the GitHub Copilot SDK) against a concrete
OpenRouter model, BYOK, at OpenRouter's own metered per-token rate — separate from the
included Copilot subscription quota. Get a key from
<https://openrouter.ai/settings/keys>.

It is **optional** — only required when a session actually requests the `openrouter`
target. If you don't use it yet, Docker still requires the referenced secret file to
exist: `touch secrets/openrouter_api_key` (mirrors the platform-you-skip pattern above).

This is a separate credential from the GitHub Copilot token itself — see "Not here: the
GitHub Copilot token" above.

## Google OAuth (M5/M8) — Calendar + Drive + Sheets + Gmail

`google_tokens/google_token.json` is **one** refresh token minted **once, locally** (no
callback server on the VPS — DESIGN), covering all four scopes (`calendar`, `drive`,
`spreadsheets`, `gmail.modify`). It is bind-mounted from `secrets/google_tokens/` into
all five Google-consuming containers — `core`, `mcp-calendar`, `mcp-drive`, `mcp-sheets`,
`mcp-gmail` (the `google` compose profile). This is the **single canonical host path**
(issue #58): no two different paths, no manual copy step.

`mcp-calendar` mounts the entire `google_tokens/` directory at `/token:ro` so additional
per-account tokens (`google_token_<label>.json`) can be added there without changing the
compose file. The other four containers mount the single file
`google_tokens/google_token.json` directly.

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
persists the refreshed token; `core` and the calendar/drive containers refresh in memory
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
