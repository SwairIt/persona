"""Admin page + endpoints for mass-resetting OCR statuses.

Lets the user push ``skipped`` / ``failed`` rows back to ``pending`` so the
OCR worker re-processes them — handy after installing Tesseract, swapping
language packs, or fixing a corrupted batch.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.ocr_admin import (
    reset_all_to_pending,
    reset_failed_to_pending,
    reset_one,
    reset_skipped_to_pending,
    status_breakdown,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr-admin"])
logger = get_logger(__name__)


@router.get("/ocr-admin", response_class=HTMLResponse)
async def ocr_admin_page(request: Request) -> HTMLResponse:
    """Render the admin dashboard with per-status counts."""
    async with get_connection() as conn:
        counts = await status_breakdown(conn)
    return templates.TemplateResponse(
        request,
        "ocr_admin.html",
        {
            "title": "OCR admin",
            "active_nav": "settings",
            "counts": counts,
        },
    )


@router.post("/ocr-admin/reset-skipped")
async def ocr_admin_reset_skipped() -> RedirectResponse:
    """Reset every ``skipped`` row back to ``pending``."""
    async with get_connection() as conn:
        affected = await reset_skipped_to_pending(conn)
    logger.info("ocr_admin.route.reset_skipped", rows=affected)
    return RedirectResponse(url="/ocr-admin", status_code=303)


@router.post("/ocr-admin/reset-failed")
async def ocr_admin_reset_failed() -> RedirectResponse:
    """Reset every ``failed`` row back to ``pending``."""
    async with get_connection() as conn:
        affected = await reset_failed_to_pending(conn)
    logger.info("ocr_admin.route.reset_failed", rows=affected)
    return RedirectResponse(url="/ocr-admin", status_code=303)


@router.post("/ocr-admin/reset-all")
async def ocr_admin_reset_all() -> RedirectResponse:
    """Reset every ``skipped`` and ``failed`` row back to ``pending``."""
    async with get_connection() as conn:
        affected = await reset_all_to_pending(conn)
    logger.info("ocr_admin.route.reset_all", rows=affected)
    return RedirectResponse(url="/ocr-admin", status_code=303)


@router.post("/api/screenshots/{screenshot_id}/reset-ocr", response_class=JSONResponse)
async def ocr_admin_reset_one(screenshot_id: int) -> JSONResponse:
    """Reset a single screenshot back to ``pending``. Returns ``{"reset": bool}``."""
    async with get_connection() as conn:
        success = await reset_one(conn, screenshot_id)
    return JSONResponse({"reset": success})
