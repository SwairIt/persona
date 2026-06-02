"""Drop the embeddings table and re-index everything from scratch.

Use after switching `PERSONA_EMBEDDINGS_MODEL` to a different vector space.
Requires the `embeddings` extra to be installed and OCR text on at least
some screenshots.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.embeddings import EmbeddingsNotAvailable, embed_texts, is_available
from app.embeddings.storage import list_unindexed_screenshots, upsert_embedding
from app.logging_setup import configure_logging, get_logger
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.rebuild_embeddings")


async def _amain(*, drop_existing: bool, batch_size: int) -> int:
    configure_logging()
    settings = get_settings()

    if not is_available():
        log.error("embeddings.unavailable", hint="uv sync --extra embeddings")
        return 1

    if drop_existing:
        async with get_connection() as conn:
            await conn.execute("DELETE FROM screenshot_embeddings")
            await conn.commit()
        log.info("rebuild.cleared")

    processed = 0
    while True:
        async with get_connection() as conn:
            pending = await list_unindexed_screenshots(
                conn,
                min_text_length=settings.embeddings_min_text_length,
                limit=batch_size,
                model=settings.embeddings_model,
            )
        if not pending:
            break
        try:
            vectors = await asyncio.to_thread(embed_texts, [p["text"] for p in pending])
        except EmbeddingsNotAvailable as exc:
            log.error("rebuild.failed", error=str(exc))
            return 1
        async with get_connection() as conn:
            for item, vec in zip(pending, vectors, strict=True):
                await upsert_embedding(
                    conn,
                    screenshot_id=item["id"],
                    vector=vec,
                    model=settings.embeddings_model,
                    text=item["text"],
                )
        processed += len(pending)
        log.info("rebuild.batch", count=processed)

    log.info("rebuild.done", processed=processed, model=settings.embeddings_model)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild the embeddings index")
    parser.add_argument("--no-drop", action="store_true", help="keep existing rows, only fill in missing ones")
    parser.add_argument("--batch", type=int, default=32, help="batch size per embed call")
    args = parser.parse_args()
    return asyncio.run(_amain(drop_existing=not args.no_drop, batch_size=args.batch))


if __name__ == "__main__":
    sys.exit(main())
