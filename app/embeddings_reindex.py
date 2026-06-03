"""Bulk re-compute embeddings for already-indexed shots.

Used after the operator changes :data:`Settings.embeddings_model` (or the
vector dimensions otherwise drift): the on-disk vectors are stale even
though :func:`app.embeddings.storage.list_unindexed_screenshots` would
still pick them up *eventually* via its model-mismatch branch. This
module forces the whole corpus through the embedder in one pass.

Walks every row in :data:`screenshots` that has OCR text long enough to
embed (controlled by :data:`Settings.embeddings_min_text_length`),
deletes the prior ``screenshot_embeddings`` row, re-computes the vector
through the existing :func:`app.embeddings.embed_texts` helper and
writes it back via :func:`app.embeddings.upsert_embedding`.

A single :func:`reindex_all` invocation is capped by ``max_shots`` so a
runaway button click cannot saturate the worker for hours on a six-figure
corpus. Progress is reported through a callback the route layer hooks
into to drive the admin polling endpoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.embeddings import (
    EmbeddingsNotAvailable,
    embed_texts,
    is_available,
    upsert_embedding,
)
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.embeddings.reindex")

# Hard upper bound: even if the caller passes a wildly higher cap we
# never touch more rows than this in a single ``reindex_all`` call. The
# admin UI also enforces a smaller default so a misclick can't trash the
# vector store.
HARD_MAX_SHOTS: int = 100_000

# Sane batch size envelope. Mirrors :data:`Settings.embeddings_batch_size`
# but kept independent so the admin job can be tuned without disturbing
# the background worker.
MIN_BATCH: int = 1
MAX_BATCH: int = 1000

# Progress callback signature — invoked after each batch with
# ``(processed, total)``. Stays sync-friendly so the route layer can
# update a plain ``dict`` without an extra event loop hop.
ProgressCallback = Callable[[int, int], None]


def _clamp(value: int, lo: int, hi: int) -> int:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


async def _count_candidates(min_text_length: int) -> int:
    """Count shots that have OCR text long enough to embed."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots "
            "WHERE ocr_status = 'done' "
            "  AND ocr_text IS NOT NULL "
            "  AND length(ocr_text) >= ?",
            (min_text_length,),
        )
        row = await cursor.fetchone()
    return 0 if row is None else int(row["n"])


async def _fetch_batch(
    *,
    min_text_length: int,
    limit: int,
    after_id: int,
) -> list[dict[str, Any]]:
    """Page through shots by ``id > after_id`` so progress is monotonic.

    Ordering by ``id`` (not ``captured_at``) makes the walk deterministic
    and resumable — the cursor stays valid even if rows are concurrently
    inserted by the capture loop.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, ocr_text FROM screenshots "
            "WHERE id > ? "
            "  AND ocr_status = 'done' "
            "  AND ocr_text IS NOT NULL "
            "  AND length(ocr_text) >= ? "
            "ORDER BY id ASC LIMIT ?",
            (after_id, min_text_length, limit),
        )
        rows = await cursor.fetchall()
    return [{"id": int(row["id"]), "text": str(row["ocr_text"])} for row in rows]


async def _clear_embeddings(ids: list[int]) -> None:
    """Drop the stored vector rows for ``ids`` so the upsert is forced.

    ``upsert_embedding`` already does an ``ON CONFLICT DO UPDATE``, so
    this step is strictly belt-and-braces — but it also means a re-index
    crash mid-batch leaves the corpus visibly empty for those rows
    rather than serving the stale vectors, which is the safer failure
    mode after a model change.
    """
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    # ``placeholders`` is "?,?,?..." built from len(ids) alone, never
    # from user input. The ids themselves bind through aiosqlite below.
    query = (
        "DELETE FROM screenshot_embeddings "  # noqa: S608 — static "?" tokens
        f"WHERE screenshot_id IN ({placeholders})"
    )
    async with get_connection() as conn:
        await conn.execute(query, tuple(ids))
        await conn.commit()


async def reindex_all(
    batch_size: int = 200,
    max_shots: int = 10_000,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Re-embed every eligible shot, capped at ``max_shots``.

    Returns a summary dict ``{"processed", "total", "batches"}`` so the
    caller can log a tidy completion line without scraping the progress
    callback. Raises :class:`EmbeddingsNotAvailable` if the fastembed
    extra is not installed — the route layer translates that into a
    friendly error state.
    """
    settings = get_settings()

    if not settings.embeddings_enabled:
        msg = "Embeddings are disabled (set PERSONA_EMBEDDINGS_ENABLED=true)"
        raise EmbeddingsNotAvailable(msg)

    if not is_available():
        msg = (
            "fastembed package not installed. Run "
            "`uv sync --extra embeddings` to enable re-indexing."
        )
        raise EmbeddingsNotAvailable(msg)

    capped_batch = _clamp(batch_size, MIN_BATCH, MAX_BATCH)
    capped_max = _clamp(max_shots, 1, HARD_MAX_SHOTS)
    min_text_length = settings.embeddings_min_text_length

    candidate_total = await _count_candidates(min_text_length)
    total = min(candidate_total, capped_max)

    log.info(
        "embeddings.reindex.start",
        model=settings.embeddings_model,
        batch_size=capped_batch,
        max_shots=capped_max,
        candidates=candidate_total,
        will_process=total,
    )

    if progress is not None:
        progress(0, total)

    processed = 0
    batches = 0
    after_id = 0

    while processed < total:
        remaining = total - processed
        limit = min(capped_batch, remaining)
        batch = await _fetch_batch(
            min_text_length=min_text_length,
            limit=limit,
            after_id=after_id,
        )
        if not batch:
            break

        ids = [item["id"] for item in batch]
        texts = [item["text"] for item in batch]

        await _clear_embeddings(ids)

        # Embedding is CPU-bound — push it off the event loop so the
        # admin status polling endpoint stays responsive.
        vectors = await asyncio.to_thread(embed_texts, texts)

        async with get_connection() as conn:
            for item, vec in zip(batch, vectors, strict=True):
                await upsert_embedding(
                    conn,
                    screenshot_id=item["id"],
                    vector=vec,
                    model=settings.embeddings_model,
                    text=item["text"],
                )

        processed += len(batch)
        batches += 1
        after_id = ids[-1]

        if progress is not None:
            progress(processed, total)

        log.info(
            "embeddings.reindex.batch",
            batch=batches,
            size=len(batch),
            processed=processed,
            total=total,
        )

    log.info(
        "embeddings.reindex.done",
        processed=processed,
        total=total,
        batches=batches,
    )

    return {"processed": processed, "total": total, "batches": batches}
