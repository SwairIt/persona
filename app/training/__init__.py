"""T23 (2026-06-08) — collect chat Q&A as the seed dataset for a
future LoRA fine-tune.

The hook in ``/api/chat/sessions/{id}/send-stream`` calls
:func:`record_qa_pair` after each assistant turn completes. Set the
``training_dataset_enabled`` kv flag to ``"0"`` to disable.

When the user reaches ~1000 pairs they can run
``/admin/dataset/export.jsonl`` to download a HuggingFace-ready file
and kick off a LoRA fine-tune on Kaggle / Colab / Vast.ai.
"""

from app.training.collector import (
    is_enabled,
    iter_export_rows,
    record_qa_pair,
    set_enabled,
    set_rating,
    stats,
)

__all__ = [
    "is_enabled",
    "iter_export_rows",
    "record_qa_pair",
    "set_enabled",
    "set_rating",
    "stats",
]
