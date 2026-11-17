# Secrets

Drop one secret per file here (no extension). Env vars of the same purpose win
(`OPENROUTER_API_KEY`, `CHIEF_WEB_PASSWORD`). **Never commit the real files**
— `.gitignore` keeps everything in this directory except this README out of
git. Files should be owner-readable only (`chmod 0600`); the wizard writes
them that way.

## Core secrets

| File | Value |
|------|-------|
| `openrouter_api_key` | OpenRouter API key (<https://openrouter.ai/settings/keys>) — the model auth; without it chief cannot chat |
| `web_password` | The web UI owner password — written by the first-run wizard (`chief wizard`); without it the web UI stays locked (fail closed) |

Both are set by the installer wizard; re-run it any time with `chief wizard`.

## MCP server secrets

A static header for an `mcp_servers` entry reads its secret from a file here (or
the upper-cased env var) by reference — `${secret:example_token}` reads
`secrets/example_token`; never put the value in `config.yaml`. An
`auth: oauth` server's login is kept by chief itself in `mcp-oauth/<server>.json`
(mode 0600) — leave it alone, and use `/logout <server>` to forget it.

## Package secrets

Packages declare the secrets they need in their manifest (e.g. Google OAuth
files); the agent asks you to place each file here during the package install
— it never asks you to paste secret values into chat.
