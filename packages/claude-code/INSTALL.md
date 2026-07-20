# Installing claude-code

Prerequisite: the Claude Code CLI installed **and logged in** on this
machine (`npm install -g @anthropic-ai/claude-code`, then the owner runs
`claude` once to complete browser OAuth). Login is interactive and
owner-owned — never attempt it yourself; if `claude -p "say ok"` errors on
auth, hand it to the owner.

Note: on macOS the CLI's credentials live in the login Keychain, which is
only unlocked in GUI login sessions — a bare SSH session may see "not
logged in" while the daemon (running in the GUI launchd domain) works
fine. Verify from chief's own shell tool, not from an SSH probe.

## Steps

1. Confirm the prerequisite from the `shell` tool: `claude --version`, then
   `claude -p "say ok" --max-turns 1` (raise the shell timeout to ~120s).
   Both must succeed before continuing.
2. Run `bash packages/claude-code/install.sh` with the `shell` tool. It
   copies the skill verbatim to `skills/claude-code/` and records the
   install in the registry (`chief.registry_apply`). No config keys.
3. `restart` — the guarded commit brings the skill live.
4. Read the installed skill once — the two rules that matter: never target
   chief's own repo, and always relay `total_cost_usd` to the owner.
