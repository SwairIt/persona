"""Background worker that turns OCR text into searchable vectors."""

from __future__ import annotations

import asyncio

from app.embeddings import (
    EmbeddingsNotAvailable,
    embed_texts,
    is_available,
    list_unindexed_screenshots,
    upsert_embedding,
)
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.embeddings_worker")

POLL_INTERVAL_SECONDS = 10.0


async def run_embeddings_worker(controller: CaptureController | None = None) -> None:
    """Continuously embed newly-OCR'd screenshots while the app is up."""
    ctrl = controller or get_controller()
    settings = get_settings()

    if not settings.embeddings_enabled:
        log.info("embeddings_worker.disabled")
        await ctrl.stop_event.wait()
        return

    if not is_available():
        log.warning(
            "embeddings_worker.dependency_missing",
            hint="install with `uv sync --extra embeddings`",
        )
        await ctrl.stop_event.wait()
        return

    log.info("embeddings_worker.started", model=settings.embeddings_model)

    while not ctrl.stop_event.is_set():
        await beat("embeddings-worker")
        try:
            await _drain_once()
        except asyncio.CancelledError:
            raise
        except EmbeddingsNotAvailable as exc:
            log.warning("embeddings_worker.unavailable", error=str(exc))
            await ctrl.stop_event.wait()
            return
        except Exception as exc:
            log.exception("embeddings_worker.iteration_failed", error=str(exc))

        try:
            await asyncio.wait_for(ctrl.stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


async def _drain_once() -> None:
    settings = get_settings()

    async with get_connection() as conn:
        pending = await list_unindexed_screenshots(
            conn,
            min_text_length=settings.embeddings_min_text_length,
            limit=settings.embeddings_batch_size,
            model=settings.embeddings_model,
        )

    if not pending:
        return

    texts = [item["text"] for item in pending]
    vectors = await asyncio.to_thread(embed_texts, texts)

    async with get_connection() as conn:
        for item, vec in zip(pending, vectors, strict=True):
            await upsert_embedding(
                conn,
                screenshot_id=item["id"],
                vector=vec,
                model=settings.embeddings_model,
                text=item["text"],
            )

    # S4b — best-effort зеркалирование в vec0 (no-op без sqlite-vec).
    try:
        from app.embeddings.vec_store import index_screenshot  # noqa: PLC0415

        for item, vec in zip(pending, vectors, strict=True):
            await index_screenshot(item["id"], vec)
    except Exception as exc:  # noqa: BLE001 — vec-зеркало не должно ломать индексацию
        log.debug("embeddings_worker.vec_mirror_failed", error=str(exc))

    log.info("embeddings_worker.indexed", count=len(pending))
