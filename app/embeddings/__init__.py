"""Optional semantic-search layer via fastembed ONNX models.

Activated when `PERSONA_EMBEDDINGS_ENABLED=true` and the `embeddings` extra
dependency group is installed (`uv sync --extra embeddings`).

The model itself is downloaded on first use (~120MB for multilingual-e5-small).
Everything runs locally on CPU; no network calls at query time.
"""

from app.embeddings.model import (
    EmbeddingsNotAvailable,
    embed_query,
    embed_texts,
    is_available,
    load_model,
)
from app.embeddings.search import semantic_search
from app.embeddings.storage import (
    decode_vector,
    encode_vector,
    fetch_embedding,
    list_unindexed_screenshots,
    upsert_embedding,
)

__all__ = [
    "EmbeddingsNotAvailable",
    "decode_vector",
    "embed_query",
    "embed_texts",
    "encode_vector",
    "fetch_embedding",
    "is_available",
    "list_unindexed_screenshots",
    "load_model",
    "semantic_search",
    "upsert_embedding",
]
