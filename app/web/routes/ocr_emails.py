"""OCR email detection — per-shot JSON badge + aggregated stats page.

Two endpoints back the v0.87 emails feature:

``GET /api/screenshot/{id}/emails.json``
    Returns ``{"emails": [...]}`` for one screenshot's OCR text. Powers the
    chip list lazy-loaded by HTMX on the screenshot detail page so the main
    render isn't paid for every shot that has no email content.

``GET /stats/emails``
    Aggregates email mentions over the last 30 days into a small Tailwind
    counter table — same shape as ``/keywords`` and ``/stats/phrases``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.ocr_emails import email_mentions, extract_emails
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

log = get_logger("persona.ocr.emails")

router = APIRouter(tags=["ocr_emails"])

# Mirror /keywords + /stats/phrases — a 30-day window is the default for all
# stats pages so the new tab doesn't surprise the user with a different scope.
_STATS_WINDOW_DAYS: int = 30


@router.get(
    "/api/screenshot/{screenshot_id}/emails.json",
    response_class=JSONResponse,
)
async def screenshot_emails_json(screenshot_id: int) -> JSONResponse:
    """Return de-duplicated emails detected in one screenshot's OCR text."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    emails = extract_emails(shot.ocr_text or "")
    log.info(
        "ocr.emails.shot_lookup",
        screenshot_id=screenshot_id,
        found=len(emails),
    )
    return JSONResponse({"emails": emails})


@router.get("/stats/emails", response_class=HTMLResponse)
async def emails_stats_page(request: Request) -> HTMLResponse:
    """Render aggregated email-mentions counter over the last 30 days."""
    items = await email_mentions(days=_STATS_WINDOW_DAYS)
    return templates.TemplateResponse(
        request,
        "emails_stats.html",
        {
            "title": "Email mentions",
            "active_nav": "stats",
            "items": items,
            "days": _STATS_WINDOW_DAYS,
        },
    )
