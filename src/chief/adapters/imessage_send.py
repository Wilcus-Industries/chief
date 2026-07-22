"""iMessage outbound: the JXA send path and the out-of-band send guard.

Split out of ``imessage.py`` (which owns the poll loop and per-thread FIFO
dispatch) so the adapter file stays inside the length cap: everything here is
about *writing* into Messages — the fixed JXA script chief's own ``send()``
runs, and the shell-tool guard that blocks any other route to the owner's
handle.
"""

import asyncio
from collections.abc import Awaitable, Callable

# Shell commands that can write into Messages behind the adapter's back. The
# adapter's own ``send()`` prefixes BOT_PREFIX so chief's replies in the shared
# self-chat are skipped on the next poll; an out-of-band sender (the ``imsg``
# CLI, raw osascript) writes an unprefixed row that polls back as an owner
# message — an infinite self-reply loop. The guard below blocks that
# mechanically; the skill-level "never imsg the owner" rule is not enough.
_OUT_OF_BAND_SENDERS = ("imsg", "osascript")


def owner_send_guard(
    owner_handles: tuple[str, ...],
) -> Callable[[str], str | None]:
    """A shell-tool guard refusing out-of-band sends to an owner handle."""
    # Lowercased for matching: Apple-ID email handles are case-insensitive,
    # so `Owner@iCloud.com` must block `imsg send --to owner@icloud.com`.
    handles = tuple(
        h.strip().lstrip("+").lower() for h in owner_handles if h.strip()
    )

    def guard(command: str) -> str | None:
        if not handles:
            return None
        lowered = command.lower()
        if not any(sender in lowered for sender in _OUT_OF_BAND_SENDERS):
            return None
        for handle in handles:
            if handle and handle in lowered:
                return (
                    "error: blocked — this command references an owner handle "
                    "via an out-of-band Messages sender (imsg/osascript). "
                    "Anything sent to the owner's own handle echoes back into "
                    "your inbox and starts an infinite self-reply loop. Answer "
                    "the owner in your normal reply instead; imsg is only for "
                    "OTHER recipients."
                )
        return None

    return guard


RunJxa = Callable[[str, tuple[str, ...]], Awaitable[str]]

SEND_TEXT_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Messages');\n"
    "  const matches = app.participants.whose({handle: argv[0]})();\n"
    "  let target = matches.length > 0 ? matches[0] : null;\n"
    "  if (target === null) {\n"
    "    const account = app.accounts.whose({serviceType: 'iMessage'})()[0];\n"
    "    target = account.participants.byId('iMessage;-;' + argv[0]);\n"
    "  }\n"
    "  app.send(argv[1], {to: target});\n"
    "  return 'sent';\n"
    "}"
)


async def run_jxa_subprocess(script: str, argv: tuple[str, ...]) -> str:
    """Run a JXA script via ``osascript``; script and argv never spliced."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-l", "JavaScript", "-e", script, *argv,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"osascript failed: {stderr.decode().strip()}")
    return stdout.decode().strip()
