"""Manual / status endpoints for the cold-storage archive."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.settings import get_settings
from app.storage.archive import archive_cold_older_than, archive_db_path, ensure_archive_schema

router = APIRouter(prefix="/api/archive", tags=["archive"])


@router.get("/status", response_class=JSONResponse)
async def archive_status() -> JSONResponse:
    settings = get_settings()
    await ensure_archive_schema()
    path = archive_db_path()
    file_size = path.stat().st_size if path.exists() else 0
    return JSONResponse(
        {
            "enabled": settings.archive_enabled,
            "archive_after_days": settings.archive_after_days,
            "archive_db": str(path),
            "archive_db_bytes": file_size,
        }
    )


@router.post("/run", response_class=JSONResponse)
async def archive_run(days: int = 0) -> JSONResponse:
    settings = get_settings()
    use_days = days or settings.archive_after_days
    if use_days < 30:
        raise HTTPException(status_code=400, detail="archive_after_days must be ≥30")
    moved = await archive_cold_older_than(use_days)
    return JSONResponse({"moved": moved, "days": use_days})
