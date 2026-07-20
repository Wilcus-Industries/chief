# TESTING — conventions and seams

The done-check, in full:

```
uv run pytest && uv run ruff check . && uv run mypy .
```

mypy runs `strict = true`. Ruff selects `E, F, I, UP, B`, target py312. Every test
carries a 30s timeout — every real test is sub-second, so a hung test fails fast.

`asyncio_mode = "auto"`, so async tests need no decorator.

## Conventions

Files are `tests/test_<module>.py`; functions are `test_<behavior_sentence>` with
long descriptive names — `test_scan_merges_roots_and_bundled_wins_collisions`,
not `test_scan_2`.

Arrange-Act-Assert. Test the error path as well as the happy path. New behavior
starts with a failing test.

## The database fixture — do not "optimize" it

**`store` gives each test its own sqlite _file_ on the production engine
(NullPool). Never change it to `StaticPool` or `:memory:`.**

A pooled fixture shares ONE connection — and therefore one transaction — across
concurrent sessions. That lets a session read another's uncommitted rows and roll
back another's write on close. It masked a real concurrency bug all the way into
main (#134). The prohibition is stated in `tests/conftest.py`,
`persistence/db.py`, and `STYLEGUIDE.md`; there is no `StaticPool` anywhere in the
tree except in those warnings.

## Fixtures — `tests/conftest.py`

| Fixture | What it gives |
|---|---|
| `engine` | `make_engine(tmp_path / "chief.db")` + schema, disposed on teardown |
| `store` | `MessageStore` over that engine |
| `sock_path` | a **short** AF_UNIX path under `/tmp` |
| `embedder` | session-scoped model2vec embedder, imported lazily |
| `vault` | a mutable copy of `tests/fixtures/vault` |

`sock_path` is short on purpose: macOS caps `sun_path` at ~104 bytes and pytest's
`tmp_path` (`/private/var/folders/...`) overflows it.

`embedder` is session-scoped and lazily imported so non-memory test runs never pay
the ~30MB first-run download.

## Testing a tool

Build a real `ToolRegistry`, register the real tool, construct a real `ToolCall`,
and `await registry.dispatch(call, context)`. `ToolContext(thread_key, channel)`
is injected only into tools with `wants_context=True`.

Dispatch **never raises** — a missing tool, bad arguments (`TypeError`), or a
handler exception all come back as error strings. Assert on the string.

`tests/test_shelltool.py` has a compact `_call()` helper worth copying.
`tests/test_mcp.py` goes further: it writes a real `FastMCP` stdio server to
`tmp_path`, connects, asserts the tool appears as `mcp_testsrv_add`, and
dispatches it end-to-end.

## The one sanctioned fake

`tests/fakes.py::FakeProvider` is "the one scripted fake CI is allowed." It
implements the provider seam so integration tests drive the **real daemon
round-trip with only the model call swapped**.

Its script is a `list[list[ProviderEvent]]`, one entry per model call. It records
`.calls` and `.models`, and exposes an optional `.gate: asyncio.Event` so
concurrency tests can control interleaving. Helpers: `text_turn(text)` and
`tool_turn(name, arguments, call_id)`.

`tests/test_app.py` is the whole-daemon seam: `build_app(config,
provider=FakeProvider(...))` → `app.start()` → a real `asyncio.open_unix_connection`
to the socket.

Mock external dependencies (network, filesystem, services) — but prefer this seam
over mocking chief's own internals.

## Don't paper over races

A turn commits its tail (spend, task status) *after* the reply reaches the IO, so
waiting on the reply and then reading the DB is a race.

**Sync on the write you assert, not a proxy for it** — wait for the committed
state (`wait_for_task_open`), never a bare `sleep`. If a poll is load-bearing for
correctness rather than for observing an async write, fix the code instead.

## Meta-tests you must keep green

- **`test_styleguide.py`** — every `src/**/*.py` under **200 lines**, unless one of
  the first 5 lines contains `styleguide: file-length`.
- **`test_architecture_docs.py`** — `docs/ARCHITECTURE.md` must exist, and the
  `self-edit` skill body must reference it. **If you restructure these docs, this
  test is the tripwire.**
- **`test_packages.py`** — real bundled manifests stay well-formed and
  `validate()` returns clean.

## Scale

44 test files, 467 test functions. The heaviest are `test_selfedit.py` (34),
`test_shelltool.py` (32), `test_config.py` (27), `test_imessage.py` (25),
`test_hooks.py` (23), `test_pkgcli.py` (22).
