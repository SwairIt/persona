"""Per-word OCR confidence overlay — v0.35.

Renders the source thumbnail with absolutely-positioned ``<span>`` tags at
each Tesseract word box, colour-coded by the per-word ``conf`` score so the
user can spot low-confidence text at a glance. Clicking a word jumps to
``/search?q=<word>`` so the operator can pivot from a single mis-recognised
token to every other screenshot that contains it.

The JSON sibling endpoint (``/api/screenshot/{id}/words.json``) returns the
same rows for clients that want to draw their own overlay (browser
extension, future companion UI, etc.).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr-overlay"])
log = get_logger(__name__)


async def _fetch_words(screenshot_id: int) -> list[dict[str, Any]]:
    """Return every stored word row for ``screenshot_id``, oldest first.

    Parametrised SQL — never f-string the id into the query. The rows are
    dicts so the template and JSON serialiser share one shape.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, word, conf, left, top, width, height "
            "FROM ocr_word "
            "WHERE screenshot_id = ? "
            "ORDER BY id ASC",
            (screenshot_id,),
        )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "word": str(row["word"]),
            "conf": int(row["conf"]),
            "left": None if row["left"] is None else int(row["left"]),
            "top": None if row["top"] is None else int(row["top"]),
            "width": None if row["width"] is None else int(row["width"]),
            "height": None if row["height"] is None else int(row["height"]),
        }
        for row in rows
    ]


@router.get("/screenshot/{screenshot_id}/overlay", response_class=HTMLResponse)
async def screenshot_overlay(request: Request, screenshot_id: int) -> HTMLResponse:
    """Render the overlay HTML page for a single screenshot."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    words = await _fetch_words(screenshot_id)
    return templates.TemplateResponse(
        request,
        "ocr_overlay.html",
        {
            "title": f"OCR overlay #{screenshot_id}",
            "active_nav": "timeline",
            "shot": shot,
            "words": words,
        },
    )


@router.get("/api/screenshot/{screenshot_id}/words.json", response_class=JSONResponse)
async def screenshot_words_json(screenshot_id: int) -> JSONResponse:
    """Return ``{screenshot_id, count, words: [...]}`` for the overlay JS."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    words = await _fetch_words(screenshot_id)
    return JSONResponse(
        {
            "screenshot_id": screenshot_id,
            "count": len(words),
            "words": words,
        }
    )
