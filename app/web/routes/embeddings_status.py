"""Status endpoint for the embeddings pipeline."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.embeddings import is_available
from app.embeddings.storage import count_embeddings
from app.settings import get_settings
from app.storage.db import get_connection

router = APIRouter(prefix="/api/embeddings", tags=["embeddings"])


@router.get("/status", response_class=JSONResponse)
async def embeddings_status() -> JSONResponse:
    settings = get_settings()

    async with get_connection() as conn:
        indexed = await count_embeddings(conn)
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots "
            "WHERE ocr_status = 'done' AND ocr_text IS NOT NULL "
            "  AND length(ocr_text) >= ?",
            (settings.embeddings_min_text_length,),
        )
        candidate_row = await cursor.fetchone()
        candidates = int(candidate_row["n"]) if candidate_row else 0

    pending = max(0, candidates - indexed)
    progress = round(indexed / candidates, 3) if candidates else 0.0

    return JSONResponse(
        {
            "enabled": settings.embeddings_enabled,
            "library_available": is_available(),
            "model": settings.embeddings_model,
            "candidates": candidates,
            "indexed": indexed,
            "pending": pending,
            "progress": progress,
        }
    )
