"""Tiny JSON endpoint that powers the live timeline 'N new captures' chip."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.storage.db import get_connection

router = APIRouter(prefix="/api/timeline", tags=["timeline-api"])


@router.get("/new-count", response_class=JSONResponse)
async def new_count(since_id: int = Query(default=0, ge=0)) -> JSONResponse:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n, MAX(id) AS max_id FROM screenshots WHERE id > ?",
            (since_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return JSONResponse({"new": 0, "max_id": since_id})
    return JSONResponse(
        {
            "new": int(row["n"]),
            "max_id": int(row["max_id"]) if row["max_id"] is not None else since_id,
        }
    )
