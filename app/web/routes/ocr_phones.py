"""OCR phone-number detection — per-shot JSON badge + aggregated stats page.

Two endpoints back the v0.88 phones feature, mirroring v0.87's emails wiring:

``GET /api/screenshot/{id}/phones.json``
    Returns ``{"phones": [...]}`` for one screenshot's OCR text. Powers the
    chip list lazy-loaded by HTMX on the screenshot detail page so the main
    render isn't paid for every shot that has no phone-number content.

``GET /stats/phones``
    Aggregates phone mentions over the last 30 days into a small Tailwind
    counter table — same shape as ``/stats/emails`` and ``/keywords``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.ocr_phones import extract_phones, phone_mentions
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

log = get_logger("persona.ocr.phones")

router = APIRouter(tags=["ocr_phones"])

# Mirror /stats/emails — a 30-day window is the default for all stats pages
# so the new tab doesn't surprise the user with a different scope.
_STATS_WINDOW_DAYS: int = 30


@router.get(
    "/api/screenshot/{screenshot_id}/phones.json",
    response_class=JSONResponse,
)
async def screenshot_phones_json(screenshot_id: int) -> JSONResponse:
    """Return de-duplicated phones detected in one screenshot's OCR text."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    phones = extract_phones(shot.ocr_text or "")
    log.info(
        "ocr.phones.shot_lookup",
        screenshot_id=screenshot_id,
        found=len(phones),
    )
    return JSONResponse({"phones": phones})


@router.get("/stats/phones", response_class=HTMLResponse)
async def phones_stats_page(request: Request) -> HTMLResponse:
    """Render aggregated phone-mentions counter over the last 30 days."""
    items = await phone_mentions(days=_STATS_WINDOW_DAYS)
    return templates.TemplateResponse(
        request,
        "phones_stats.html",
        {
            "title": "Phone mentions",
            "active_nav": "stats",
            "items": items,
            "days": _STATS_WINDOW_DAYS,
        },
    )
