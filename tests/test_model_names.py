"""Model-name validation: a typo must not wedge a thread onto a dead model."""

import pytest

from chief.provider.model_names import UnknownModelError, validate_model_name

ALIASES = frozenset({"opus", "sonnet"})


def check(model: str) -> str:
    return validate_model_name(model, aliases=ALIASES, default_model="qwen/q3")


def test_configured_alias_passes() -> None:
    assert check("opus") == "opus"


def test_default_model_passes() -> None:
    assert check("qwen/q3") == "qwen/q3"


def test_qualified_backend_id_passes() -> None:
    assert check("anthropic/claude-sonnet-5") == "anthropic/claude-sonnet-5"


def test_surrounding_whitespace_is_stripped() -> None:
    assert check("  opus  ") == "opus"


def test_bare_unknown_name_is_rejected() -> None:
    # The live bug: "/model sonnet" with no sonnet alias reached OpenRouter as
    # a model id and 400'd every subsequent turn in the thread.
    with pytest.raises(UnknownModelError) as excinfo:
        validate_model_name(
            "sonnet", aliases=frozenset({"opus"}), default_model="qwen/q3"
        )
    message = str(excinfo.value)
    assert "sonnet" in message
    assert "opus" in message, "the error must list the names that do work"


def test_empty_name_is_rejected() -> None:
    with pytest.raises(UnknownModelError):
        check("   ")
