"""OpenAI-shape transcript -> Anthropic Messages API request body.

The provider seam's contract is the OpenAI dict shape, so the Anthropic backend
translates at the edge: every ``system`` message (leading prompt, compaction
note) is lifted into the top-level ``system`` field in order; assistant
``tool_calls`` become ``tool_use`` blocks; consecutive ``tool`` messages fold
into one user turn of ``tool_result`` blocks (Anthropic requires every result
of a parallel round in a single user message).
"""

from typing import Any

from chief.provider.base import ToolSpec, parse_arguments

# USD per million (input, output) tokens. The Messages API reports tokens, not
# dollars, so without this the budget cap would be inert on this backend. An
# unlisted model costs 0 — the same caveat a zero-cost proxy carries.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of one call by the price table; 0 for an unlisted model."""
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def to_anthropic_request(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[ToolSpec],
    *,
    max_tokens: int,
) -> dict[str, Any]:
    """Build a streaming ``/v1/messages`` body from a chief transcript."""
    system = [str(m.get("content") or "") for m in messages if m["role"] == "system"]
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": _turns([m for m in messages if m["role"] != "system"]),
    }
    if system:
        body["system"] = "\n\n".join(system)
    if tools:
        body["tools"] = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
    return body


def _turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "tool":
            result = {
                "type": "tool_result",
                "tool_use_id": message["tool_call_id"],
                "content": str(message.get("content") or ""),
            }
            if turns and _is_result_turn(turns[-1]):
                turns[-1]["content"].append(result)
            else:
                turns.append({"role": "user", "content": [result]})
        elif message["role"] == "assistant" and message.get("tool_calls"):
            turns.append({"role": "assistant", "content": _assistant_blocks(message)})
        else:
            turns.append({"role": message["role"], "content": message.get("content")})
    return turns


def _is_result_turn(turn: dict[str, Any]) -> bool:
    content = turn["content"]
    return (
        turn["role"] == "user"
        and isinstance(content, list)
        and all(block.get("type") == "tool_result" for block in content)
    )


def _assistant_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if text := message.get("content"):
        blocks.append({"type": "text", "text": str(text)})
    for call in message["tool_calls"]:
        function = call["function"]
        blocks.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": function["name"],
                "input": _parse_input(function.get("arguments")),
            }
        )
    return blocks



def _parse_input(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    return parse_arguments(str(arguments or ""))
