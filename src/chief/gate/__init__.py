"""The permission gate — classify every tool call, default-ask on effect (M3).

DESIGN's spine is *enforce in code, never in prompt*: one classifier decides allow/deny/
ask for every tool call, and everything effectful that isn't pre-approved routes through
the owner approval round-trip. Everything downstream (calendar/shell/Gmail tools at
M5–M8) reuses this gate rather than re-implementing safety.
"""
