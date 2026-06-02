"""Admin UI for the per-app OCR skip-list.

Lets the user disable OCR for noisy apps (terminals, video players,
games) whose text content adds nothing but garbage to the searchable
index. The OCR worker consults this list before invoking Tesseract.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.storage.db import get_connection
from app.storage.ocr_skip import add, list_skipped, remove
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr"])


@router.get("/settings/ocr-skip", response_class=HTMLResponse)
async def ocr_skip_page(request: Request) -> HTMLResponse:
    skipped = await list_skipped()
    skipped_set = {item.casefold() for item in skipped}
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT app_name FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name != '' "
            "ORDER BY app_name LIMIT 200"
        )
        rows = await cursor.fetchall()
    suggestions = [
        str(row["app_name"])
        for row in rows
        if str(row["app_name"]).strip().casefold() not in skipped_set
    ]
    return templates.TemplateResponse(
        request,
        "ocr_skip.html",
        {
            "title": "OCR skip-list",
            "active_nav": "settings",
            "skipped": skipped,
            "suggestions": suggestions,
        },
    )


@router.post("/settings/ocr-skip")
async def ocr_skip_create(app_name: str = Form(...)) -> RedirectResponse:
    try:
        await add(app_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/ocr-skip", status_code=303)


@router.post("/settings/ocr-skip/{app_name}/delete")
async def ocr_skip_delete(app_name: str) -> RedirectResponse:
    await remove(app_name)
    return RedirectResponse(url="/settings/ocr-skip", status_code=303)
