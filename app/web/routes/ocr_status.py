"""Lightweight OCR-pipeline status — for the header badge."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.ocr import is_available
from app.settings import get_settings
from app.storage.db import get_connection

router = APIRouter(prefix="/api/ocr", tags=["ocr"])


@router.get("/status", response_class=JSONResponse)
async def ocr_status() -> JSONResponse:
    settings = get_settings()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ocr_status, COUNT(*) AS n FROM screenshots GROUP BY ocr_status"
        )
        rows = await cursor.fetchall()
    counts = {str(row["ocr_status"]): int(row["n"]) for row in rows}
    total = sum(counts.values())
    done = counts.get("done", 0)
    pending = counts.get("pending", 0)
    skipped = counts.get("skipped", 0)
    failed = counts.get("failed", 0)

    return JSONResponse(
        {
            "enabled": settings.ocr_enabled,
            "available": is_available(settings.tesseract_path),
            "total": total,
            "done": done,
            "pending": pending,
            "skipped": skipped,
            "failed": failed,
            "progress": round(done / total, 3) if total else 0.0,
        }
    )
