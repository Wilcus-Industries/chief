"""Model-name validation shared by the ``/model`` command and ``switch_model``.

Both entry points used to record whatever string they were handed. The router
(:mod:`chief.provider.router`) sends any non-alias name to the *default*
backend unchanged, so a bare typo like ``sonnet`` reached OpenRouter as a model
id and every later turn in that thread failed with HTTP 400 — the override is
persisted, so the thread stayed wedged until someone set a valid model again.

A name is accepted when it is a configured alias, the default model, or looks
like a backend-qualified id (``vendor/model``). That last shape is what the
default backend actually accepts; a bare unknown word never resolves anywhere,
so rejecting it up front costs nothing and turns a wedged thread into a typo
message.
"""


class UnknownModelError(ValueError):
    """Raised for a model name that could not route anywhere."""


def validate_model_name(
    model: str, *, aliases: frozenset[str], default_model: str
) -> str:
    """Return ``model`` unchanged, or raise :class:`UnknownModelError`.

    ``aliases`` are the configured ``provider_aliases`` keys.
    """
    name = model.strip()
    if not name:
        raise UnknownModelError("model name is empty")
    if name in aliases or name == default_model or "/" in name:
        return name
    known = ", ".join(sorted(aliases | {default_model})) or "(none configured)"
    raise UnknownModelError(
        f"unknown model {name!r} — it is not a configured alias and has no "
        f"'vendor/model' prefix, so no backend would accept it. "
        f"Known names: {known}."
    )
