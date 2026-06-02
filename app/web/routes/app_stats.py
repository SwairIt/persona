"""Per-app analytics — `/apps/{name}`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.storage.db import get_connection
from app.storage.repository import list_screenshots
from app.storage.time import iso
from app.web.templates_engine import templates

router = APIRouter(tags=["app-stats"])


@router.get("/apps", response_class=HTMLResponse)
async def apps_index(request: Request) -> HTMLResponse:
    """Index of every app, sorted by total screenshots, with 14-day sparkline."""
    from datetime import date as date_cls
    from datetime import timedelta

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n, MIN(captured_at) AS first_seen, MAX(captured_at) AS last_seen "
            "FROM screenshots WHERE app_name IS NOT NULL "
            "GROUP BY app_name ORDER BY n DESC"
        )
        rows = await cursor.fetchall()
        cursor = await conn.execute(
            "SELECT app_name, DATE(captured_at) AS day, COUNT(*) AS n "
            "FROM screenshots WHERE app_name IS NOT NULL "
            "AND captured_at >= DATE('now', '-13 days') "
            "GROUP BY app_name, day"
        )
        spark_rows = await cursor.fetchall()

    today = date_cls.today()
    axis = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    raw: dict[str, dict[str, int]] = {}
    for row in spark_rows:
        raw.setdefault(str(row["app_name"]), {})[str(row["day"])] = int(row["n"])

    apps = [
        {
            "name": str(row["app_name"]),
            "count": int(row["n"]),
            "first_seen": str(row["first_seen"]),
            "last_seen": str(row["last_seen"]),
            "spark": [raw.get(str(row["app_name"]), {}).get(d, 0) for d in axis],
        }
        for row in rows
    ]
    return templates.TemplateResponse(
        request,
        "apps_index.html",
        {"title": "Apps", "active_nav": "stats", "apps": apps},
    )


@router.get("/apps/{name}", response_class=HTMLResponse)
async def app_detail(request: Request, name: str, days: int = Query(default=30, ge=1, le=365)) -> HTMLResponse:
    since = datetime.now(timezone.utc) - timedelta(days=days)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n, MIN(captured_at) AS first_seen, MAX(captured_at) AS last_seen "
            "FROM screenshots WHERE app_name = ?",
            (name,),
        )
        meta_row = await cursor.fetchone()
        if meta_row is None or int(meta_row["n"]) == 0:
            raise HTTPException(status_code=404, detail=f"App not found: {name}")

        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n FROM screenshots "
            "WHERE app_name = ? AND captured_at >= ? GROUP BY day ORDER BY day",
            (name, iso(since)),
        )
        per_day_rows = await cursor.fetchall()

        cursor = await conn.execute(
            "SELECT window_title, COUNT(*) AS n FROM screenshots "
            "WHERE app_name = ? AND window_title IS NOT NULL AND window_title != '' "
            "GROUP BY window_title ORDER BY n DESC LIMIT 20",
            (name,),
        )
        top_titles_rows = await cursor.fetchall()

        latest_shots = await list_screenshots(conn, limit=24, app_name=name)

    return templates.TemplateResponse(
        request,
        "app_detail.html",
        {
            "title": f"App · {name}",
            "active_nav": "stats",
            "app_name": name,
            "days": days,
            "total": int(meta_row["n"]),
            "first_seen": str(meta_row["first_seen"]),
            "last_seen": str(meta_row["last_seen"]),
            "per_day": [
                {"day": str(row["day"]), "count": int(row["n"])} for row in per_day_rows
            ],
            "top_titles": [
                {"title": str(row["window_title"]), "count": int(row["n"])}
                for row in top_titles_rows
            ],
            "latest": latest_shots,
        },
    )
