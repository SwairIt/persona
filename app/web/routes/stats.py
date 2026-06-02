"""Statistics dashboard — events/day, top apps, dedup ratio, disk usage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.analysis import compute_streaks, per_day_total_seconds
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.size_log import list_recent, sample_today, today_bytes
from app.storage.tiers import count_by_tier
from app.storage.time import iso
from app.web.templates_engine import templates
from app.workers.control import get_controller

router = APIRouter(tags=["stats"])


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request) -> HTMLResponse:
    payload = await _collect_stats()
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "title": "Stats",
            "active_nav": "stats",
            **payload,
        },
    )


@router.get("/stats.json", response_class=JSONResponse)
async def stats_json() -> JSONResponse:
    payload = await _collect_stats()
    serialisable = {
        **payload,
        "events_by_day": payload["events_by_day"],
        "top_apps": payload["top_apps"],
        "disk_usage_bytes": payload["disk_usage_bytes"],
    }
    return JSONResponse(serialisable)


async def _collect_stats() -> dict[str, object]:
    settings = get_settings()
    controller = get_controller()

    cutoff = datetime.now(timezone.utc) - timedelta(days=14)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n FROM screenshots "
            "WHERE captured_at >= ? GROUP BY day ORDER BY day",
            (iso(cutoff),),
        )
        events_by_day_rows = await cursor.fetchall()

        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE captured_at >= ? AND app_name IS NOT NULL "
            "GROUP BY app_name ORDER BY n DESC LIMIT 12",
            (iso(cutoff),),
        )
        top_apps_rows = await cursor.fetchall()

        cursor = await conn.execute(
            "SELECT window_title, app_name, COUNT(*) AS n FROM screenshots "
            "WHERE captured_at >= ? AND window_title IS NOT NULL AND window_title != '' "
            "GROUP BY window_title ORDER BY n DESC LIMIT 12",
            (iso(cutoff),),
        )
        top_windows_rows = await cursor.fetchall()

        cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshots")
        total_screenshots_row = await cursor.fetchone()
        total_screenshots = int(total_screenshots_row["n"]) if total_screenshots_row else 0

        cursor = await conn.execute("SELECT COUNT(*) AS n FROM dedup_groups")
        total_groups_row = await cursor.fetchone()
        total_groups = int(total_groups_row["n"]) if total_groups_row else 0

        cursor = await conn.execute(
            "SELECT ocr_status, COUNT(*) AS n FROM screenshots GROUP BY ocr_status"
        )
        ocr_rows = await cursor.fetchall()
        ocr_breakdown = {str(row["ocr_status"]): int(row["n"]) for row in ocr_rows}

        cursor = await conn.execute(
            "SELECT CAST(strftime('%H', captured_at) AS INTEGER) AS hour, "
            "       CAST(strftime('%w', captured_at) AS INTEGER) AS dow, "
            "       COUNT(*) AS n "
            "FROM screenshots WHERE captured_at >= ? "
            "GROUP BY hour, dow",
            (iso(cutoff),),
        )
        heatmap_rows = await cursor.fetchall()

    heatmap = [[0] * 24 for _ in range(7)]
    for row in heatmap_rows:
        dow = int(row["dow"])
        hour = int(row["hour"])
        if 0 <= dow < 7 and 0 <= hour < 24:
            heatmap[dow][hour] = int(row["n"])

    disk_usage_bytes = _measure_thumbnails_dir(settings.thumbnails_dir)

    async with get_connection() as conn:
        await sample_today(conn, settings.thumbnails_dir)
        bytes_today = await today_bytes(conn)
        size_history = await list_recent(conn, days=14)
        tiers = await count_by_tier(conn)
        streak = await compute_streaks(conn)
        year_activity = await per_day_total_seconds(conn, days=365)

    budget_bytes = int(settings.daily_size_budget_mb * 1024 * 1024)

    return {
        "captures_total": controller.captures_total,
        "captures_skipped_dedup": controller.captures_skipped_dedup,
        "captures_skipped_idle": controller.captures_skipped_idle,
        "captures_failed": controller.captures_failed,
        "last_capture_at": controller.last_capture_at,
        "paused": controller.paused,
        "events_by_day": [
            {"day": row["day"], "count": int(row["n"])} for row in events_by_day_rows
        ],
        "top_apps": [
            {"app": row["app_name"], "count": int(row["n"])} for row in top_apps_rows
        ],
        "top_windows": [
            {
                "title": row["window_title"],
                "app": row["app_name"],
                "count": int(row["n"]),
            }
            for row in top_windows_rows
        ],
        "total_screenshots": total_screenshots,
        "total_groups": total_groups,
        "dedup_ratio": (
            round(1 - total_groups / total_screenshots, 3) if total_screenshots else 0.0
        ),
        "ocr_breakdown": ocr_breakdown,
        "disk_usage_bytes": disk_usage_bytes,
        "retention_days": settings.retention_days,
        "interval_seconds": settings.capture_interval_seconds,
        "heatmap": heatmap,
        "today_bytes": bytes_today,
        "budget_mb": settings.daily_size_budget_mb,
        "budget_bytes": budget_bytes,
        "size_history": size_history,
        "tiers": tiers,
        "warm_after_days": settings.tier_warm_after_days,
        "cold_after_days": settings.tier_cold_after_days,
        "smart_thumbnail": settings.smart_thumbnail,
        "smart_min_gap_seconds": settings.smart_min_gap_seconds,
        "streak_current": streak.current_streak,
        "streak_longest": streak.longest_streak,
        "streak_active_30d": streak.active_days_30d,
        "streak_active_total": streak.active_days_total,
        "year_activity": year_activity,
    }


def _measure_thumbnails_dir(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*.webp"):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total
