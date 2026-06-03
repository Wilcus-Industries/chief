# CLAUDE.md

> **Read first:** [`STYLEGUIDE.md`](./STYLEGUIDE.md) — code conventions, follow on every change.

Project-specific rules only. The universal working rules — boundaries, when-stuck,
secrets, done-honesty — live in the global `~/.claude/CLAUDE.md` and apply underneath
this. Only add a rule here when it differs from, or isn't covered by, the global.

## Definition of done

The project's full check — the global done-rule points here for the exact commands. All
must pass before any task is "done":

```
uv run pytest
uv run ruff check .
uv run mypy .
```

## Keep these docs current

Treat `CLAUDE.md` and `STYLEGUIDE.md` as living docs, not write-once boilerplate. As part
of a change that makes a rule here stale, wrong, or redundant, prune or rewrite it in the
same change; add a rule when a real, recurring need shows up. Keep it tight — fewer,
sharper lines beat an accreting pile. (This project directive overrides the global
ask-first-before-editing-docs rule for these two files.)
