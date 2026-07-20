# STYLEGUIDE.md

Code conventions for this repo. Follow these on every change. CLAUDE.md covers
workflow; this file covers how code is written.

## Naming & structure

- **Naming:** follow the conventions of the language and the surrounding code. Prefer
  descriptive names over abbreviations (`order_count`, not `ordc`). All names, comments,
  and docs in English.
- **Function length:** keep functions short (~50 lines). If one grows past that, extract a helper.
- **File length (STRICT):** production files stay **under 200 lines — hard cap**; aim for
  **under 100**. The cap is CI-enforced by the done-check; tests are exempt. A file may
  exceed 200 only when splitting is genuinely worse, and must carry a
  `# styleguide: file-length — <justification>` comment at the top to pass the check.
- **Files:** keep modules short and single-responsibility. A file should have one clear
  reason to exist; split when it accumulates unrelated concerns.
- **Self-documenting code:** prefer clear names and small functions over explanatory
  comments. Reach for a comment only when the code can't speak for itself.

## Comments, docs & typing

- **Comments explain WHY, not WHAT.** Don't restate the code. Comment business rules,
  non-obvious decisions, and trade-offs — things a reader can't recover from the code alone.
- **Document the public surface.** Every public/exported function, class, and module gets
  a short doc describing its contract (purpose, args, returns, errors). Internal helpers
  rely on names + types instead.
- **Type your interfaces.** Annotate public signatures where the language supports it.
  Avoid escape hatches (`any`, untyped casts); if you must, justify it in a comment.
- **No dead code.** Remove unused functions, imports, variables, and commented-out blocks
  immediately. Version control is the backup.

## Error handling & robustness

- **Be specific, never swallow.** Catch the narrowest error that fits; never catch-all and
  hide it. Don't silently ignore a caught error — handle it, re-raise, or log with context.
- **Logging:** use the project's logger, not stray prints. Log errors with enough context
  to debug (what operation, what inputs, what failed).
- **Idiomatic & lint-clean:** write code that passes `uv run ruff check .` and the formatter
  without manual fixups. Aim for code that's already clean as written.

## Testing

- **Framework:** `pytest`. Run with `uv run pytest`.
- **Test-first:** new behavior starts with a failing test (the loop in CLAUDE.md).
- **Cover new reusable logic** with tests.
- **Test both paths:** happy path *and* error conditions (bad input, raised errors, edge
  cases) — not just success.
- **Structure:** Arrange-Act-Assert. Mock external dependencies (network, filesystem,
  services) so tests are fast and deterministic.
- **The `engine` / `store` fixtures give each test its own sqlite *file*** on the
  production engine (NullPool). Do not "optimize" them back to StaticPool / `:memory:`:
  a pooled fixture shares ONE connection — and so one transaction — across concurrent
  sessions, letting a session read another's uncommitted rows and roll back another's
  write on close. That masked a real concurrency bug all the way into main (#134).
- **Sync on the write you assert, not on a proxy for it.** A turn commits its tail
  (spend, task status) *after* the reply reaches the IO, so waiting on the reply and
  then reading the DB is a race. Wait for the committed state (`wait_for_task_open`).
  Never paper over a race with a bare `sleep` — if a poll is load-bearing for
  correctness rather than for observing an async write, fix the code instead.
