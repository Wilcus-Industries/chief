---
name: claude-code
description: Delegate a coding task on another project to the Claude Code CLI (one-shot print mode via the shell tool) — when the owner asks for real dev work on a repo that isn't chief.
---

# claude-code — delegating coding work

`claude` (the Claude Code CLI, already on this machine) is an autonomous
coding agent you can hand a whole task to. Use it when the owner asks for
substantive dev work on some **other** project directory — a fix, a
feature, a review — that would take you many shell round-trips to do by
hand.

**Never point it at chief's own repo.** Your self-edit pipeline (done-check,
guarded commit, restart gate) is the only sanctioned way chief's code
changes; a second agent writing here bypasses all of it.

## One-shot print mode (the only mode you use)

```
cd /path/to/project && claude -p "Fix the failing tests in src/parser" \
  --output-format json --max-turns 15
```

Always through the **shell tool**, always with a raised `timeout` (300–900s
— these runs take minutes; a killed run wastes the spend). Interactive/tmux
sessions are not for you: no PTY babysitting.

The JSON result carries `result` (the report), `subtype`
(`success` / `error_max_turns` / `error_budget`), `num_turns`,
`total_cost_usd`, and `session_id`. **Relay `total_cost_usd` to the owner**
with the outcome — delegation spends real money.

Follow-up on the same task: `claude -p "now add tests" --resume
<session_id> --max-turns 10` (same directory).

Read-only analysis (review, explain) — cap the blast radius:

```
cd /path/to/project && claude -p "Review this diff for bugs" \
  --allowedTools "Read,Grep,Glob" --output-format json --max-turns 8
```

## Rules

- **Owner-directed only**: delegate exactly the task the owner asked for,
  on the directory they named. Never pick targets yourself.
- **Report honestly**: pass back the result summary + cost; on
  `error_max_turns`, say it ran out rather than pretending completion.
- Don't stack flags you don't understand; plain `-p` + `--output-format
  json` + `--max-turns` covers nearly everything.
- Never use `--dangerously-skip-permissions`.
- The CLI must already be logged in (it is, via the machine's existing
  setup). `claude -p` erroring with auth/login output means the login needs
  the owner's hands — tell them; don't attempt logins yourself.
- If `claude` is missing (`command not found`), this package's premise is
  gone — say so instead of npm-installing anything.
