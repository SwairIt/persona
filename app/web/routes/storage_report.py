"""``GET /storage-report`` — per-day storage breakdown UI."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage_report import daily_breakdown
from app.web.templates_engine import templates

router = APIRouter(tags=["storage-report"])

logger = get_logger(__name__)

_DEFAULT_DAYS = 30


@router.get("/storage-report", response_class=HTMLResponse)
async def storage_report_page(request: Request) -> HTMLResponse:
    """Render the day-by-day storage report with sparkline + table."""
    settings = get_settings()
    rows = await daily_breakdown(days_back=_DEFAULT_DAYS)

    grand_total_bytes = sum(row["total_bytes"] for row in rows)
    grand_thumbnails_bytes = sum(row["thumbnails_bytes"] for row in rows)
    grand_ocr_bytes = sum(row["ocr_bytes"] for row in rows)
    grand_screenshots = sum(row["screenshots"] for row in rows)
    avg_total_bytes = (grand_total_bytes // len(rows)) if rows else 0

    budget_bytes = int(settings.daily_size_budget_mb * 1024 * 1024)

    return templates.TemplateResponse(
        request,
        "storage_report.html",
        {
            "title": "Storage report",
            "active_nav": "stats",
            "rows": rows,
            "days_window": _DEFAULT_DAYS,
            "grand_total_bytes": grand_total_bytes,
            "grand_thumbnails_bytes": grand_thumbnails_bytes,
            "grand_ocr_bytes": grand_ocr_bytes,
            "grand_screenshots": grand_screenshots,
            "avg_total_bytes": avg_total_bytes,
            "budget_mb": settings.daily_size_budget_mb,
            "budget_bytes": budget_bytes,
        },
    )


__all__ = ["router"]
