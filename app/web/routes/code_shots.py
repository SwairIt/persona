"""Browse-page for shots whose OCR was heuristically flagged as code.

Backed by the partial index on ``screenshots.ocr_looks_like_code = 1``
from migration ``149`` so the listing is cheap even on large corpora.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["code-shots"])
log = get_logger("persona.code_shots")

_MAX_LIMIT = 500


async def _fetch_code_shots(days: int, limit: int) -> list[dict[str, object]]:
    sql = (
        "SELECT id, captured_at, app_name, window_title, thumbnail_path, "
        "       substr(coalesce(ocr_text, ''), 1, 200) AS ocr_preview "
        "FROM screenshots "
        "WHERE ocr_looks_like_code = 1 "
        "  AND captured_at >= datetime('now', ?) "
        "ORDER BY captured_at DESC "
        "LIMIT ?"
    )
    async with get_connection() as conn:
        cursor = await conn.execute(sql, (f"-{days} days", limit))
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]),
            "app_name": row["app_name"],
            "window_title": row["window_title"],
            "thumbnail_path": row["thumbnail_path"],
            "ocr_preview": row["ocr_preview"] or "",
        }
        for row in rows
    ]


@router.get("/code-shots", response_class=HTMLResponse)
async def code_shots_page(
    request: Request,
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(100, ge=1, le=_MAX_LIMIT),
) -> HTMLResponse:
    """Grid view of shots whose OCR looks like source code."""
    shots = await _fetch_code_shots(days=days, limit=limit)
    return templates.TemplateResponse(
        request,
        "code_shots.html",
        {
            "shots": shots,
            "days": days,
            "limit": limit,
            "title": "Code shots",
            "active_nav": "search",
        },
    )


@router.get("/api/code-shots.json", response_class=JSONResponse)
async def code_shots_json(
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(100, ge=1, le=_MAX_LIMIT),
) -> JSONResponse:
    """Machine-readable variant of the browse page."""
    shots = await _fetch_code_shots(days=days, limit=limit)
    return JSONResponse({"days": days, "limit": limit, "shots": shots})
