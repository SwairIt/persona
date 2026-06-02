"""Weekly digest — high-level recap, no LLM required."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.storage.db import get_connection
from app.storage.time import iso
from app.web.templates_engine import templates

router = APIRouter(prefix="/digest", tags=["digest"])


@router.get("/weekly", response_class=HTMLResponse)
async def weekly(request: Request, weeks_ago: int = Query(default=0, ge=0, le=52)) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    end = now - timedelta(days=7 * weeks_ago)
    start = end - timedelta(days=7)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ? GROUP BY day ORDER BY day",
            (iso(start), iso(end)),
        )
        per_day = await cursor.fetchall()

        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ? AND app_name IS NOT NULL "
            "GROUP BY app_name ORDER BY n DESC LIMIT 10",
            (iso(start), iso(end)),
        )
        top_apps = await cursor.fetchall()

        cursor = await conn.execute(
            "SELECT n.screenshot_id, n.body, n.updated_at, s.app_name, s.window_title "
            "FROM screenshot_notes n JOIN screenshots s ON s.id = n.screenshot_id "
            "WHERE s.captured_at >= ? AND s.captured_at < ? "
            "ORDER BY n.updated_at DESC LIMIT 20",
            (iso(start), iso(end)),
        )
        notes = await cursor.fetchall()

        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots WHERE captured_at >= ? AND captured_at < ?",
            (iso(start), iso(end)),
        )
        total_row = await cursor.fetchone()
        total = int(total_row["n"]) if total_row else 0

    days_active = sum(1 for r in per_day if int(r["n"]) > 0)
    busiest = max(per_day, key=lambda r: int(r["n"]), default=None)

    return templates.TemplateResponse(
        request,
        "digest_weekly.html",
        {
            "title": "Weekly digest",
            "active_nav": "digest",
            "start": start,
            "end": end,
            "weeks_ago": weeks_ago,
            "total": total,
            "days_active": days_active,
            "per_day": [{"day": str(r["day"]), "count": int(r["n"])} for r in per_day],
            "top_apps": [
                {"app": str(r["app_name"]), "count": int(r["n"])} for r in top_apps
            ],
            "busiest": (
                {"day": str(busiest["day"]), "count": int(busiest["n"])} if busiest else None
            ),
            "notes": [
                {
                    "id": int(r["screenshot_id"]),
                    "body": str(r["body"]),
                    "app_name": r["app_name"],
                    "window_title": r["window_title"],
                    "updated_at": str(r["updated_at"]),
                }
                for r in notes
            ],
        },
    )
