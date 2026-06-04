"""Storage budget status panel (v1.13).

Shows today's per-bucket byte usage against the daily cap, the projected
end-of-day total, and the current throttle level. Surfaced as JSON for
the dashboard widget and as a tiny HTML strip for power users who want
to see it standalone.

The numbers here come from :mod:`app.budget`; this module is
*display-only* and does no enforcement of its own.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.budget import _project_eod, get_throttle_level, get_today_bytes
from app.settings import get_settings

router = APIRouter(tags=["budget"])


@router.get("/api/budget/today.json")
async def budget_today() -> JSONResponse:
    """Return per-bucket usage + projection + throttle level for today."""
    cfg = get_settings()
    buckets = await get_today_bytes()
    total = sum(buckets.values())
    projected = await _project_eod()
    level = await get_throttle_level()
    cap_bytes = int(cfg.daily_budget_mb * 1024 * 1024)
    return JSONResponse(
        {
            "cap_bytes": cap_bytes,
            "cap_mb": cfg.daily_budget_mb,
            "total_bytes": total,
            "projected_bytes": projected,
            "buckets": buckets,
            "throttle_level": level,
            "enforcer_enabled": cfg.budget_enforcer_enabled,
        },
    )
