"""The model2vec embedder, loaded once per model name and reused everywhere.

The ambient hook builds a fresh index each firing; reloading the static model's
weights every turn would waste seconds and memory, so one loaded model per name
is cached at module scope. model2vec imports lazily on first load.
"""

from typing import Any

_MODEL_CACHE: dict[str, Any] = {}


def load_model(name: str) -> Any:
    """Return the cached static model for ``name``, loading it on first use."""
    if name not in _MODEL_CACHE:
        from model2vec import StaticModel

        _MODEL_CACHE[name] = StaticModel.from_pretrained(name)
    return _MODEL_CACHE[name]


def embed(model: Any, texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` into plain float lists chromadb accepts."""
    return [vector.tolist() for vector in model.encode(texts)]
