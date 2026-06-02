"""Thin wrapper around fastembed — keeps the model in-memory once loaded."""

from __future__ import annotations

from threading import Lock
from typing import Any

from app.settings import get_settings


class EmbeddingsNotAvailable(RuntimeError):
    """Raised when fastembed is not installed or embeddings are disabled."""


_model: Any = None
_model_lock = Lock()


def is_available() -> bool:
    """True if the optional fastembed dependency can be imported."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return True


def load_model() -> Any:
    """Lazy-load the configured fastembed model. Thread-safe singleton."""
    global _model
    if _model is not None:
        return _model

    settings = get_settings()
    if not settings.embeddings_enabled:
        msg = "Embeddings are disabled (set PERSONA_EMBEDDINGS_ENABLED=true)"
        raise EmbeddingsNotAvailable(msg)

    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        msg = (
            "fastembed package not installed. Run "
            "`uv sync --extra embeddings` to enable semantic search."
        )
        raise EmbeddingsNotAvailable(msg) from exc

    with _model_lock:
        if _model is None:
            cache_dir = settings.data_dir / "models"
            cache_dir.mkdir(parents=True, exist_ok=True)
            _model = TextEmbedding(
                model_name=settings.embeddings_model,
                cache_dir=str(cache_dir),
            )
    return _model


def reset_model() -> None:
    """Test helper to drop the cached model."""
    global _model
    _model = None


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of passages. Returns list of float32 vectors."""
    if not texts:
        return []
    model = load_model()
    prefixed = [f"passage: {t}" for t in texts]
    return [list(map(float, v)) for v in model.embed(prefixed)]


def embed_query(text: str) -> list[float]:
    """Embed a single search query."""
    if not text.strip():
        msg = "Empty query"
        raise ValueError(msg)
    model = load_model()
    prefixed = f"query: {text}"
    result = next(iter(model.embed([prefixed])))
    return [float(x) for x in result]
