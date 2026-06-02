"""Size-budget endpoint — today's bytes, last 14 days, tier breakdown."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.size_log import list_recent, sample_today, today_bytes
from app.storage.tiers import count_by_tier

router = APIRouter(prefix="/api/budget", tags=["budget"])


@router.get("/status", response_class=JSONResponse)
async def budget_status() -> JSONResponse:
    settings = get_settings()
    async with get_connection() as conn:
        await sample_today(conn, settings.thumbnails_dir)
        bytes_today = await today_bytes(conn)
        recent = await list_recent(conn, days=14)
        tiers = await count_by_tier(conn)

    budget_bytes = int(settings.daily_size_budget_mb * 1024 * 1024)
    used_pct = round(bytes_today / budget_bytes, 4) if budget_bytes else 0.0

    return JSONResponse(
        {
            "today_bytes": bytes_today,
            "today_mb": round(bytes_today / 1024 / 1024, 2),
            "budget_mb": settings.daily_size_budget_mb,
            "budget_bytes": budget_bytes,
            "used_percent": round(used_pct * 100, 1),
            "over_budget": bytes_today > budget_bytes if budget_bytes else False,
            "recent_days": recent,
            "smart_thumbnail": settings.smart_thumbnail,
            "smart_min_gap_seconds": settings.smart_min_gap_seconds,
            "tiered_retention": settings.tiered_retention,
            "tiers": tiers,
            "warm_after_days": settings.tier_warm_after_days,
            "cold_after_days": settings.tier_cold_after_days,
        }
    )
