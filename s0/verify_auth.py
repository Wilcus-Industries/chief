"""S0a — prove Claude Max auth runs the Agent SDK headless.

Throwaway de-risking slice (DESIGN.md S0). Fires a one-shot SDK query using the
``CLAUDE_CODE_OAUTH_TOKEN`` minted by ``claude setup-token`` and prints the result
telemetry that shows the call ran on the subscription credit rather than the API.

Exits non-zero on any failure so it doubles as a container/CI gate.
"""

import os
import sys

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

PROMPT = "Reply with exactly one word: pong"


def check_auth_env() -> None:
    """Fail fast unless the env is set up to bill the subscription, not the API.

    ``ANTHROPIC_API_KEY`` outranks the OAuth token in the SDK's auth precedence, so
    its mere presence would silently route spend to pay-as-you-go API rates — the
    exact failure mode S0 exists to rule out.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY is set — it outranks CLAUDE_CODE_OAUTH_TOKEN and would "
            "bill the API instead of your Max subscription. Unset it before running S0."
        )
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        sys.exit(
            "CLAUDE_CODE_OAUTH_TOKEN not set — run `claude setup-token` and export it."
        )


async def main() -> None:
    check_auth_env()

    options = ClaudeAgentOptions(
        system_prompt="You are a terse test fixture.",
        max_turns=1,
    )

    reply_parts: list[str] = []
    result: ResultMessage | None = None
    async for message in query(prompt=PROMPT, options=options):
        if isinstance(message, AssistantMessage):
            reply_parts += [b.text for b in message.content if isinstance(b, TextBlock)]
        elif isinstance(message, ResultMessage):
            result = message

    reply = "".join(reply_parts).strip()
    if not reply:
        sys.exit("No assistant text returned — auth or SDK wiring failed.")
    if result is None:
        sys.exit("No ResultMessage returned — the SDK stream ended abnormally.")
    if result.is_error:
        sys.exit(f"SDK reported an error result: {result.result!r}")

    print(f"assistant reply: {reply!r}")
    print(f"total_cost_usd:  {result.total_cost_usd}")
    print(f"model_usage:     {result.model_usage}")
    print(f"session_id:      {result.session_id}")
    print(f"num_turns:       {result.num_turns}")
    print("\nS0a OK — the SDK ran headless on CLAUDE_CODE_OAUTH_TOKEN.")
    print("Cross-check Console usage: spend should draw the Agent SDK credit, not API.")


if __name__ == "__main__":
    anyio.run(main)
