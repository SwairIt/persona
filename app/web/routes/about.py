"""Feature-status dashboard — quick overview of which optional bits are turned on."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app import __version__
from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.embeddings import is_available as embeddings_available
from app.ocr import probe_tesseract
from app.settings import get_settings
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["about"])


@router.get("/about", response_class=HTMLResponse)
async def about_page(
    request: Request,
    _user: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    settings = get_settings()
    tesseract = probe_tesseract(settings.tesseract_path)

    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshots")
        row = await cursor.fetchone()
        total_screenshots = int(row["n"]) if row else 0

        cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshot_notes")
        row = await cursor.fetchone()
        total_notes = int(row["n"]) if row else 0

        cursor = await conn.execute("SELECT COUNT(*) AS n FROM tags")
        row = await cursor.fetchone()
        total_tags = int(row["n"]) if row else 0

        cursor = await conn.execute("SELECT COUNT(*) AS n FROM private_vault")
        row = await cursor.fetchone()
        total_private = int(row["n"]) if row else 0

        cursor = await conn.execute("SELECT COUNT(*) AS n FROM webhooks WHERE enabled = 1")
        row = await cursor.fetchone()
        total_webhooks = int(row["n"]) if row else 0

    features = [
        {
            "name": "Screen capture",
            "on": True,
            "detail": f"every {settings.capture_interval_seconds:g}s, {total_screenshots} captured",
        },
        {
            "name": "OCR (Tesseract)",
            "on": tesseract.available and settings.ocr_enabled,
            "detail": (
                f"binary: {tesseract.version}" if tesseract.available else "binary missing"
            ),
        },
        {
            "name": "Semantic search",
            "on": settings.embeddings_enabled and embeddings_available(),
            "detail": (
                f"model: {settings.embeddings_model}"
                if settings.embeddings_enabled
                else "off in .env"
            ),
        },
        {
            "name": "BYO LLM",
            "on": bool(settings.byo_api_key and settings.byo_api_provider),
            "detail": settings.byo_api_provider or "no provider",
        },
        {
            "name": "Auto daily digest",
            "on": settings.auto_digest_enabled,
            "detail": f"fires at {settings.auto_digest_hour_local:02d}:00 local",
        },
        {
            "name": "Tiered retention",
            "on": settings.tiered_retention,
            "detail": (
                f"hot {settings.tier_warm_after_days}d → warm "
                f"{settings.tier_cold_after_days}d → cold"
            ),
        },
        {
            "name": "Smart thumbnail",
            "on": settings.smart_thumbnail,
            "detail": f"min gap {settings.smart_min_gap_seconds:g}s, budget {settings.daily_size_budget_mb} MB/day",
        },
        {
            "name": "Multi-monitor",
            "on": settings.multi_monitor,
            "detail": "captures every connected display",
        },
        {
            "name": "Archive (cold→SQLite)",
            "on": settings.archive_enabled,
            "detail": f"after {settings.archive_after_days} days",
        },
        {
            "name": "Private vault",
            "on": total_private > 0,
            "detail": f"{total_private} encrypted screenshot{'s' if total_private != 1 else ''}",
        },
        {
            "name": "Webhooks",
            "on": total_webhooks > 0,
            "detail": f"{total_webhooks} active subscriber{'s' if total_webhooks != 1 else ''}",
        },
    ]

    return templates.TemplateResponse(
        request,
        "about.html",
        {
            "title": "About",
            "active_nav": "settings",
            "version": __version__,
            "features": features,
            "total_screenshots": total_screenshots,
            "total_notes": total_notes,
            "total_tags": total_tags,
        },
    )
